# PRD dan checklist WA Control & Live

Gunakan [arah dan aturan pengembangan](AGENTS.md) bersama
[status kandidat terbaru](STATUS.md).

## Hasil yang ingin dicapai

WA Control & Live adalah satu plugin komunitas untuk Hermes Agent yang
menggabungkan WhatsApp Control, chatbot tamu per akun, dan telepon Live untuk
pemilik. Pengguna GitHub bisa memasang plugin di profil Hermes baru, menautkan
akun dari terminal, memilih cara login dan provider yang tersedia bagi mereka,
lalu mengatur WhatsApp dan audio Live dari Hermes.

- **A adalah nomor bot Hermes.** Satu koneksi Rust untuk akun A melayani DM
  pemilik, Control, dan telepon Live. Wizard mendeteksi A dari pairing
  WhatsApp Hermes bila sudah ada. Penautan plugin memverifikasi akun yang
  memindai QR Live cocok dengan A. Satu penautan A berlaku untuk ketiga fungsi.
- **B adalah nomor pribadi yang dipakai pemilik untuk menghubungi A.** Pemilik
  bisa menautkan B secara terpisah untuk mengontrol akun pribadinya. B juga bisa
  tetap dipakai hanya untuk menghubungi A. Akun Control tambahan memakai Baileys
  dan identitas diambil dari hasil scan. Cegah A masuk sebagai akun tambahan
  dan minta konfirmasi sebelum memberikan izin Live atau DM pemilik.
- Pemilik bisa memilih A, B, beberapa akun, atau semua akun yang dikelola.
  Nomor B dan akun tambahan tidak perlu ikut ditautkan untuk menggunakan Live
  pada A. Jumlah akun mengikuti akun yang ditautkan dan kemampuan WhatsApp.
- Teks dan telepon melanjutkan percakapan WhatsApp Hermes yang sedang aktif.
  Sesi baru, sesi lama yang dilanjutkan, pesan teks saat menelepon, transkrip,
  konteks lampiran, dan pekerjaan Hermes mengikuti percakapan itu. Provider
  audio bisa diganti sambil mempertahankan sesi Hermes dan panggilan WhatsApp.
  Target penerimaan adalah perilaku yang sama dari sudut pandang pemilik
  dalam [skenario ChatGPT Live Desktop](#acuan-perilaku-telepon).
- Kode inti Hermes mengikuti upstream. Gunakan API publik untuk lifecycle,
  setup, perintah, dan kredensial. Catat penggunaan API internal pada integrasi
  sesi dan uji ulang pada setiap build Hermes yang dinyatakan didukung.

## Acuan perilaku telepon

ChatGPT Live di aplikasi Desktop menjadi pembanding untuk pengalaman
pemilik. Panduan OpenAI menjelaskan percakapan yang bisa disela, penggunaan
konteks tugas yang sudah ada, dan kemampuan berdiskusi, mengecek progres atau
mengubah arahan sambil pekerjaan berjalan. Artikel teknis OpenAI menjelaskan
bahwa aliran suara tetap aktif selama delegasi berjalan secara asinkron.
OpenClaw Talk dan ElevenAgents juga mendokumentasikan tanggapan singkat
yang bisa diucapkan sebelum pekerjaan selesai. Tanggapan awal menyebut
rencana dengan bahasa yang alami. Progres yang ditanyakan mengikuti status
pekerjaan yang sedang aktif.
Panduan Desktop menjelaskan tombol penutupan untuk pengguna. Kemampuan
asisten menutup telepon lewat percakapan merupakan syarat pemilik untuk
plugin ini. Cocokkan cara dan waktunya lewat pembandingan sebelum membuat
aturan seperti menutup sendiri saat pekerjaan selesai atau saat hening.

| Skenario | Perilaku yang harus dialami pemilik |
| --- | --- |
| Mulai dari DM, lalu telepon | Hermes melanjutkan percakapan yang sama, dengan identitas, konteks, lampiran, dan pekerjaan aktif dari DM. Sesudah telepon, lanjutkan lewat DM yang sama. |
| Kirim pesan biasa ke DM Hermes | WhatsApp menampilkan indikator mengetik dari siklus bawaan Hermes saat agen menyiapkan balasan. |
| Minta pekerjaan yang butuh waktu, lalu beralih ke obrolan biasa | Hermes menyebut satu langkah konkret yang akan ia lakukan sambil tugas mulai diproses. Model suara langsung menjawab obrolan baru selama pekerjaan berjalan. Ucapan biasa atau pertanyaan progres tidak mengubah, mengulang, atau membatalkan tugas. Saat ditanya apa yang sedang dilakukan, Hermes menjelaskan langkah terbaru yang terverifikasi dari tugas yang sama. |
| Minta Hermes mencari sesuatu dan mengirimkannya ke WhatsApp | Hermes dapat menyebut singkat langkahnya sendiri, misalnya bahwa ia akan mencari dulu lalu mengirimkannya ke WhatsApp. Status sudah selesai dan sudah dikirim mengikuti bukti masing-masing. |
| Jawaban langsung dan jeda setelahnya | Hermes menanggapi inti ucapan dengan nada yang mengikuti obrolan, tanpa pembuka generik di setiap giliran. Setelah menjawab, ia memberi ruang kepada pemilik untuk bicara. Hasil baru dan kendala yang perlu ditangani tetap dapat disampaikan. |
| Langkah gagal saat tugas masih berjalan | Live memakai kejadian terverifikasi dari langkah yang sedang dikerjakan. Jika halaman melaporkan pembuatan gagal, sampaikan temuan itu tanpa menunggu balasan akhir di DM. Saat ditanya, pakai progres terbaru dan jelaskan bila langkah atau persentasenya belum diketahui. |
| Mulai bicara saat asisten sedang menjawab | Audio jawaban lama berhenti saat pemilik menyela, termasuk saat model selesai menghasilkan jawaban tetapi audio masih antre. Panggilan menerima ucapan baru. Tugas aktif tetap berjalan. |
| Beri arahan baru untuk tugas tertentu | Cocokkan tugas yang dimaksud dan gunakan jalur perubahan arahan Hermes. Permintaan berhenti bicara, permintaan mengakhiri telepon, dan permintaan membatalkan tugas adalah tindakan yang berbeda. |
| Minta asisten mengakhiri panggilan saat kerja masih berjalan | Asisten meminta WhatsApp benar-benar menutup panggilan setelah maksud pemilik terverifikasi. Salam penutup boleh selesai atau dilewati jika audio tidak tersedia. Ucapan susulan dapat menyela penutupan, dan tugas tetap berjalan di sesi DM yang sama. |
| Hasil atau kebutuhan persetujuan muncul saat sedang mengobrol | Sampaikan status yang benar tanpa merebut giliran pemilik. Sebut kebutuhan persetujuan saat Hermes benar-benar memerlukannya dan gunakan mekanisme persetujuan asli Hermes. |

Bedakan akhir produksi suara di provider, audio yang dikirim sidecar, dan
audio yang terdengar di perangkat. Respons yang disela tidak boleh dianggap
telah disampaikan seluruhnya. Pemotongan transkrip sampai kata terakhir
dan label khusus untuk jawaban terpotong adalah opsi tampilan jika bukti
audio dan pembandingan mendukungnya. Ketepatan per kata bukan syarat
penerimaan Live. Periksa bahwa percakapan lanjutan tidak menganggap bagian
yang belum disampaikan sebagai sesuatu yang sudah didengar pemilik.

Jalankan skenario yang sama di Desktop dan WhatsApp dengan satu pekerjaan
uji yang cukup lama. Catat versi, provider, waktu mulai jawaban dan waktu
audio terakhir terdengar setelah disela, penutupan yang benar-benar terjadi,
serta status dan identitas tugas sebelum dan sesudah. Pengujian simulasi
menetapkan kontrak dan menangkap regresi. Klaim tentang kelancaran dan
kesetaraan pengalaman memerlukan pengamatan kedua permukaan.

[Panduan ChatGPT Voice Desktop](https://learn.chatgpt.com/docs/features/voice)
· [Arsitektur suara GPT-Live](https://openai.com/index/continuous-voice-interaction-with-gpt-live/)
· [Gaya bicara GPT-Live saat memakai alat](https://developers.openai.com/api/docs/guides/live-prompting#personality-during-tool-calls)
· [Waktu menyuarakan hasil di Gemini Live](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/asynchronous-function-calling)
· [Percakapan saat pekerjaan berjalan di LiveKit](https://docs.livekit.io/agents/logic/patterns/subagent-delegation/#maintaining-coherence)
· [OpenClaw Talk realtime](https://docs.openclaw.ai/nodes/talk/realtime-sessions)
· [Orkestrasi percakapan ElevenAgents](https://elevenlabs.io/blog/unpacking-elevenagents-orchestration-engine)
· [Panduan evaluasi GPT-Live](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation)

## Pemilik dan chatbot tamu

- Dari DM pemilik, perintah natural ke Hermes dapat memilih akun dan mengatur
  pesan, file, Status, grup, serta kebijakan chatbot. Izin mengikuti akun
  dan permintaan pemilik yang diverifikasi.
- Chatbot tamu memakai instruksi yang dapat diubah untuk tiap akun. Pilihan
  DM dan grup dapat dinonaktifkan, dibatasi ke daftar yang diizinkan, atau
  dibuka sesuai konfigurasi. Grup dapat mensyaratkan mention atau menerima
  balasan tanpa mention pada grup yang dikonfigurasi.
- Model tamu memakai konteks chat tersebut sesuai jumlah pesan terakhir yang
  disetel. Sebagai contoh, pemilik dapat memilih 20 pesan atau menonaktifkan
  riwayat. Akses alat, konteks pemilik, dan riwayat chat lain tetap terpisah
  dari model tamu.
- Model boleh mengenali maksud untuk berbicara dengan manusia dalam bahasa
  natural. Durasi jeda handoff bisa disetel. Setelah jeda habis atau pemilik
  melanjutkannya, tanggapi pesan baru yang memenuhi aturan chat.
- Dukungan media tamu mencakup baca gambar, transkripsi voice note, dan
  balasan teks atau voice note sesuai pengaturan dan kemampuan provider.
  [Panduan kebijakan chatbot](whatsapp_control/README.md)

## Setup publik dan perintah singkat

Target pengalaman instalasi dari repositori publik Hermes melalui terminal:

```sh
hermes plugins install OWNER/REPO --enable
hermes wa-control-live pair
```

Paket Git membawa sidecar macOS arm64 yang diverifikasi terhadap checksum
dan sumber paket. Hermes menyiapkan dependensi Python dan Node dengan
persetujuan standar di profil yang sudah menjalankan bot. Satu wizard
`pair` mendeteksi bot, mengonfirmasi izin pemilik, menampilkan QR Live
untuk A, lalu menawarkan QR akun Control tambahan di terminal yang sama.
Jika gateway sudah berjalan, restart lewat Hermes sekali agar adapter
plugin dipakai pada profil dan percakapan yang sama.
`whatsapp-control pair <label>` tetap tersedia untuk menambah akun nanti
atau mengulangi QR yang gagal, termasuk saat gateway dijeda. Penautan saat
gateway dijeda hanya membuka koneksi sementara untuk QR. `setup` juga
tersedia untuk mengubah pilihan audio.

Setup menawarkan **pakai konfigurasi Hermes** dan **siapkan Live manual**.
Pengguna memilih provider dan model yang mendukung suara Live, suara bila
tersedia, serta sumber login atau secret. Codex CLI hanya menjadi prasyarat
jika pengguna memilih jalur Codex CLI. Masukan kredensial mengikuti mekanisme
secret profil Hermes. UI status menampilkan alias dan kesiapan kredensial.

| Keperluan | Terminal | WhatsApp atau Telegram |
| --- | --- | --- |
| Lihat pilihan | `hermes wa-control-live list` | `/walive list` |
| Lihat status | `hermes wa-control-live status` | `/walive` atau `/walive status` |
| Pilih untuk koneksi berikutnya | `hermes wa-control-live use gemini` | `/walive use gemini` |
| Ganti sekarang | `hermes wa-control-live use gemini now` | `/walive use gemini now` |

Nama `gemini` hanya contoh alias. `use` juga menerima alias pool. Setup menyimpan
model dan suara yang didukung per alias. Pemilik bisa memilih kombinasi tersebut
lewat alias dalam permintaan natural atau lewat `/walive`, untuk panggilan berikutnya
atau panggilan aktif. Terminal, slash, dan permintaan natural memakai operasi
pemilihan yang sama. Tool saat ini menerima alias dan pilihan waktu; penyebutan
ID model atau nama suara saja belum diterjemahkan ke pilihan atau perubahan alias.

Simpan pilihan, entri provider, dan pool di `plugins.entries.wa-control-live.settings.live`
per profil Hermes. Gunakan penyimpanan pengaturan atomik dari Hermes. Perintah
`status` menyatukan kesehatan layanan, provider yang tersambung, dan pilihan
untuk koneksi berikutnya. Pemilihan provider cukup dibaca ulang saat membuka
koneksi audio. Pairing WhatsApp tetap dipakai.

`/model` tetap memilih model Hermes untuk percakapan dan tugas. `/walive`
mengatur audio Live. Handler slash memeriksa identitas dari event gateway.
Tool percakapan memakai bukti pemilik dan turn Hermes yang sudah ada. Di
WhatsApp gunakan DM pemilik yang terverifikasi. Di Telegram gunakan identitas
yang diizinkan untuk mengelola profil tersebut. Uji perilaku slash saat Hermes
sedang bekerja karena plugin mengikuti penanganan input sibuk di gateway.

[Manajemen plugin Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#managing-plugins)
· [API CLI dan slash Hermes](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#register-slash-commands)
· [Pengaturan OpenClaw Talk](https://docs.openclaw.ai/nodes/talk)

## Lifecycle resmi Hermes

Repo publik memakai ID `wa-control-live` dan manifest `plugin.yaml` di
root. Hermes mengelola instalasi, aktivasi, pembaruan, dan penghapusan kode
plugin pada profil yang dipilih. Plugin mengelola proses Control dan Live,
perangkat tertaut, dan data runtime miliknya.

| Tindakan | Perintah dan hasil yang diharapkan |
| --- | --- |
| Install | `hermes plugins install OWNER/REPO --enable` memasang paket dari GitHub lewat prompt dependensi Hermes. Petunjuk bawaan instalasi menampilkan `hermes wa-control-live pair`. Opsi `--no-enable` memasang dalam keadaan nonaktif. |
| Enable | `hermes plugins enable wa-control-live` memulihkan akses plugin di profil. Saat layanan dijalankan kembali, gunakan pairing yang masih valid. |
| Disable | `hermes plugins disable wa-control-live` menonaktifkan integrasi plugin. Launcher menghentikan proses yang dimiliki plugin. Simpan konfigurasi dan pairing agar bisa dipakai saat diaktifkan kembali. |
| Update | `hermes plugins update wa-control-live` mengikuti sumber instalasi yang tidak dipin. Untuk instalasi yang dipin, gunakan `hermes plugins install OWNER/REPO --force --ref <SHA-40-karakter>` untuk memilih revisi baru. Verifikasi pairing dan pengaturan sesudah update. |
| Uninstall | `hermes plugins uninstall wa-control-live` adalah alias resmi `remove`. Hermes menghapus kode yang diinstal, registrasi, dan entri konfigurasi plugin. Pairing dan data runtime disimpan di luar direktori kode. Setelah instalasi ulang di profil yang sama, pengaturan yang dihapus perlu diisi kembali, lalu pairing yang masih valid dapat dipakai kembali. |
| Hapus data dan uninstall | Jalankan `hermes wa-control-live cleanup --purge` saat plugin masih terpasang dan aktif. Setelah hasil cleanup diperiksa, jalankan `hermes plugins uninstall wa-control-live`. Perintah `hermes whatsapp-control cleanup --purge` memakai implementasi yang sama. |

Ikuti hasil reload gateway yang dilaporkan Hermes. Lakukan restart gateway
untuk melepaskan hook platform yang sudah terpasang ketika pengujian
menemukannya. Uji dari sesi baru dan periksa bahwa proses aktif Control, Rust,
dan adapter audio yang dimiliki plugin benar-benar berhenti. Perintah
`cleanup` biasa meminta dan menunggu penghentian proses yang dimiliki plugin,
lalu melaporkan bahwa data lokal disimpan.

Pada purge, hentikan penerimaan pekerjaan baru, upayakan melepas perangkat
tertaut selagi transportnya bisa dihubungi, lalu hapus data lokal milik
plugin setelah proses berhenti. Catat hasil lokal dan konfirmasi WhatsApp
secara terpisah untuk setiap akun, termasuk bot yang sudah tertaut sebelum
terdaftar di Control. Untuk akun yang pelepasannya belum terkonfirmasi,
tampilkan cara melepasnya dari menu Perangkat Tertaut. Hapus pairing,
pengaturan akun lokal, riwayat chatbot, log, dependensi hasil setup, dan
runtime warisan yang dibuat oleh instalasi ini. Pertahankan riwayat Hermes,
kredensial provider yang dipakai bersama, pairing WhatsApp bawaan Hermes,
dan data instalasi atau profil lain.

Purge menargetkan data Control dan kedua lokasi Live pada instalasi ini.
Laporan perangkat membedakan bot gabungan, sesi Live lama, dan perangkat
Baileys. Jika instalasi Control standalone berbagi penyimpanan pada profil
yang sama, cleanup menolak penghapusan bersama. Hermes menangani penghapusan
kode dan entri konfigurasi pada langkah uninstall.
[Lifecycle resmi Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#managing-plugins)
· [Penyimpanan state plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#store-durable-state)
· [Cleanup kandidat](whatsapp_control/cli.py)
· [Lifecycle launcher](spikes/003-whatsapp-call/launch.py)

## Provider dan autentikasi

Live memakai satu alur `LiveCall` untuk percakapan WhatsApp yang sedang
aktif, konteks DM, transkrip, interupsi, dan pekerjaan Hermes. Pemilihan
provider, akun, model, dan suara menentukan konektor audio yang dipakai oleh
alur ini. Setiap konektor menangani autentikasi dan protokolnya, lalu
menerjemahkan audio, giliran, dan konfirmasi pengiriman ke masukan yang
dipakai bersama. Konektor melaporkan konfirmasi sesuai bukti protokol.
Perubahan sesi DM mengikuti sesi Hermes yang baru aktif. Perpindahan
provider atau akun dalam sesi yang sama mengganti koneksi audio pada
`LiveCall` yang sedang berlangsung.

| Jalur yang direncanakan | Setup dan kriteria kompatibilitas |
| --- | --- |
| Codex Realtime | Pakai login akun ChatGPT di Codex CLI melalui app-server. Teruskan pilihan model dan suara yang didukung jalur itu. |
| Login Codex di Hermes | `hermes-realtime` memakai broker publik Hermes untuk meminta token sesi OpenAI Realtime dari profil aktif, lalu membuka audio WebRTC. Verifikasi kelayakan model dan audio pada akun uji sebelum menyatakan jalur ini siap untuk rilis. |
| OpenAI GPT-Live | Pakai API key proyek untuk GPT-Live. Delegasi pekerjaan kepada Hermes melalui mode client delegation. |
| Gemini Live | Pakai secret Gemini dari profil Hermes atau masukkan secret baru melalui setup. Pilih model Live dan suara. Hubungkan transport Gemini ke jalur sesi WA yang sama. |
| Provider Hermes atau gateway kustom | Pakai kembali nama dan referensi kredensial jika cocok. Lengkapi protokol, endpoint audio dua arah, model, dan suara. Buktikan pembukaan sesi dan audio masuk serta keluar pada endpoint yang dipilih. |

Pengaturan Custom Endpoint di Hermes bisa menyediakan nama, URL, format
permintaan, dan referensi kredensial. WA Control & Live menyimpan rincian
protokol audio di pengaturannya sendiri. Jalur OAuth memakai login resmi atau
broker Hermes sesuai kemampuan provider dan endpoint yang digunakan. Chat dan
tugas Hermes tetap memakai provider profil, termasuk gateway seperti 9Router.
Gateway kustom baru tercatat siap untuk audio Live setelah kontrak audio dua
arah teruji.

[Model provider Hermes](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin)
· [Broker login Codex Hermes](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#use-the-users-codex-sign-in-core-made-requests)
· [Delegasi GPT-Live](https://developers.openai.com/api/docs/guides/live-delegation)
· [Protokol Gemini Live](https://aistudio.google.com/api-reference/live/bidi-generate-content)

## Rotasi dan perpindahan saat telepon

1. **Rotasi saat membuka panggilan.** Pengguna boleh memilih satu entri atau
   pool dengan urutan provider yang dipilih. Jika sebuah provider punya
   beberapa akun atau key, gunakan giliran yang tersendiri untuk anggota
   provider tersebut. Pilih konektor dan kredensial yang siap untuk setiap
   panggilan. Catat posisi rotasi per profil secara atomik. Saat anggota
   tidak tersedia, ikuti urutan pool dan hormati informasi jeda dari provider.
   Lima belas key Gemini hanya contoh. Key dalam satu proyek Gemini berbagi
   kuota proyek.
2. **Pindah di panggilan yang sama.** Permintaan eksplisit seperti `use gemini now`
   atau kegagalan koneksi menjalankan perpindahan ke pilihan yang diminta atau
   kandidat fallback dari pool yang dikonfigurasi. Sambungan WhatsApp dan sesi
   Hermes dipertahankan. Sambungkan calon provider pada `LiveCall` yang sama
   dengan konteks terbaru dari Hermes, termasuk transkrip yang sudah final
   dan ID pekerjaan yang sudah diterima. Sinkronkan giliran yang baru selesai
   selama persiapan, lalu
   arahkan audio ke calon setelah siap. Satu koneksi menerima aliran audio
   sebagai koneksi aktif. Ukur jeda dari audio terakhir koneksi lama hingga
   audio pertama koneksi pengganti.
3. **Tangani hasil yang tertunda.** Simpan ucapan final dan status penyampaian
   yang dapat dibuktikan. Tafsirkan event terlambat menurut koneksi dan giliran
   asalnya. Lanjutkan pekerjaan yang sudah diterima berdasarkan bukti operasi.
   Hasil yang datang selama perpindahan atau setelah tutup telepon tetap
   tersedia di percakapan Hermes yang sama.
4. **Tangani kegagalan perpindahan.** Jika koneksi lama masih sehat, pertahankan
   audio itu sampai pengganti siap. Jika koneksi lama sudah putus, lakukan
   pemulihan lewat kandidat pool dan laporkan hasil yang benar. Tangani kondisi
   seluruh kandidat tidak tersedia sambil menjaga riwayat dan pekerjaan.
   Saat ada panggilan, `use <alias> now` menyimpan pilihan baru dan
   melaporkan sukses setelah audio baru siap. Saat panggilan tidak aktif,
   perintah memilih alias untuk panggilan berikutnya. Fallback otomatis
   memilih anggota lain untuk panggilan tersebut dan tetap mengikuti pool
   yang dipilih pengguna.
5. **Bedakan resumption dan pindah provider.** Resumption bawaan dapat
   dipertimbangkan untuk sesi dan kredensial yang sama jika provider
   mendukungnya. Untuk kredensial atau provider lain, buat sesi audio pada
   tujuan dan isi konteks dari Hermes.
   Jika rotasi ada di gateway eksternal, plugin memilih endpoint gateway
   tersebut dan gateway mengelola akun yang disembunyikan di belakangnya.

9Router menjadi acuan rotasi akun per permintaan. `LiveCall` sudah menyiapkan
pengganti sebelum mematikan audio lama yang sehat. Saat audio putus, kandidat
yang tersedia dicoba dulu. Jika kandidat habis karena gangguan sementara, satu
percobaan ulang ke akun yang sama dapat membuka sesi audio baru setelah jeda
retry, selama masih dalam batas waktu pemulihan panggilan. Sesi baru mengambil
konteks dari Hermes. Resumption Gemini dengan handle dan fork GPT-Live adalah
pilihan teknis yang masih terbuka. Keduanya memiliki syarat penyimpanan di
sisi provider.

[Rotasi akun 9Router](https://github.com/decolua/9router/blob/v0.5.119/src/sse/services/auth.js#L131-L181)
· [Kuota Gemini](https://aistudio.google.com/docs/rate-limits)
· [Resumption Gemini](https://aistudio.google.com/docs/live-api/session-management#session-resumption)
· [Retensi Gemini Live](https://aistudio.google.com/docs/zdr)
· [Pemulihan sesi GPT-Live](https://developers.openai.com/api/docs/guides/live-conversations#recover-from-a-failed-connection)
· [Loop audio WA saat ini](spikes/003-whatsapp-call/call_adapter.py)

## BRD perilaku normal dan kegagalan

### Penyebab dan pemulihan provider Live

Laporkan berdasarkan bukti dari adaptor provider. Gunakan kode dan detail
terstruktur dari respons pembukaan, event saat sesi berjalan, atau alasan
penutupan yang didokumentasikan. Pisahkan kegagalan Live, model teks Hermes,
koneksi WhatsApp, dan runtime lokal. Pembukaan WebSocket baru dianggap siap
untuk dipakai setelah konfirmasi setup provider dan kesiapan jalur media.
Uji audio dua arah untuk menyatakan jalur itu didukung pada rilis.

| Kondisi yang diketahui | Tindakan dan pesan untuk pemilik |
| --- | --- |
| Pembatasan sementara atau kuota mencapai batas | Lewati anggota yang sedang dibatasi dan coba anggota berikutnya dalam pool. Hormati waktu tunggu minimum dari `Retry-After`, petunjuk retry terstruktur, atau waktu reset yang memang diketahui untuk batas yang relevan. Gunakan jeda bertahap dengan jitter dan batas percobaan bagi pembatasan sementara tanpa petunjuk. Saat semua anggota kena limit, beri tahu penyebab dan waktu paling awal untuk mencoba jika tersedia. Tandai waktu perkiraan lokal sebagai perkiraan. |
| Kredit, billing, atau batas belanja memerlukan tindakan | Tampilkan alias dan tindakan di proyek atau akun provider. Coba anggota sehat lainnya. Untuk lingkup akun/proyek yang sama dan penyebab yang terkonfirmasi, tunggu perubahan atau reset yang dilaporkan provider. |
| Login OAuth kedaluwarsa atau dicabut | Gunakan refresh resmi untuk konektor yang mendukungnya. Jika refresh ditolak secara definitif atau provider meminta interaksi, beri tahu jalur login asal, seperti Codex CLI atau akun profil Hermes. Pilih anggota sehat selama menunggu. Uji koneksi Live setelah pengguna login kembali. |
| API key kosong, ditolak, atau akses proyek dibatasi | Arahkan pemilik memperbarui secret di setup asal atau memeriksa izin proyek dan model. Tandai alias sampai konfigurasi diperbaiki. Bedakan key yang ditolak dengan izin yang ditolak ketika provider memberi bukti. Masukkan kembali alias setelah validasi. |
| Endpoint, model, suara, atau protokol audio tidak didukung | Tunjukkan pengaturan yang perlu dikoreksi. Pertahankan pilihan lama yang masih valid. Tawarkan alias lain yang sudah dikonfigurasi jika diperlukan. |
| Koneksi provider putus, layanan kelebihan beban, atau sesi audio mencapai batas durasi | Coba sambung ulang pada sesi yang sama jika didukung, atau siapkan pengganti dengan konteks Hermes. Batasi waktu pemulihan dan jumlah percobaan. Tangani Gemini `GoAway` dan GPT-Live `expired` sesuai siklus sesi. |
| Provider menolak satu giliran atau menutup sesi karena kebijakan | Gunakan keterangan penolakan yang memang tersedia. Jika provider tetap menerima input, lanjutkan percakapannya. Jika sesi ditutup, laporkan dan hentikan percobaan otomatis untuk input yang ditolak. |
| Tersedia hanya kode penutupan umum, error gateway generik, atau koneksi terputus tanpa detail | Laporkan bahwa koneksi gagal dan penyebab provider belum diketahui. Coba pemulihan yang aman jika tersedia. Gunakan label seperti “koneksi audio gagal” sampai ada bukti yang lebih spesifik. |

Satu status HTTP bisa mewakili beberapa kondisi. Kode `429` bisa membawa
pembatasan sementara, kuota, atau masalah kredit. Terapkan status bersama
pada beberapa alias hanya ketika lingkup kuota yang sama bisa dibuktikan.
Jika semua anggota gagal dengan sebab berbeda, laporkan “semua provider
Live tidak tersedia” dan jelaskan tindakan untuk tiap alias. Kode diagnostik,
waktu, dan ID permintaan yang aman bisa ditampilkan di `status`. Kredensial
ditangani lewat setup dan penyimpanan secret di profil yang bersangkutan.

[Error Gemini](https://ai.google.dev/gemini-api/docs/api-errors)
· [Kode error OpenAI](https://developers.openai.com/api/docs/guides/error-codes)
· [Siklus dan error GPT-Live](https://developers.openai.com/api/docs/guides/live-conversations#handle-errors-and-end-the-session)
· [Akun Codex app-server](https://learn.chatgpt.com/docs/app-server#1-check-auth-state)
· [Format Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)

### Notifikasi sebelum dan selama telepon

| Tahap | Jika tersedia | Jika gagal |
| --- | --- | --- |
| Pemilik minta ditelepon | Periksa kandidat sebelum menelepon. Laporkan permintaan diterima, mulai menelepon, terjawab, dan audio siap sesuai kejadian yang diketahui. | Jika semua kandidat diketahui belum siap, akhiri permintaan sebelum membuat dering dan balas di percakapan pemilik. Jika panggilan sudah berdering atau sudah diangkat ketika kegagalan diketahui, gunakan notifikasi tahap yang sesuai. |
| Bot menerima telepon pemilik | Siapkan koneksi audio selama dering jika transport mendukung, lalu jawab dengan jalur yang siap. | Jika semua kandidat gagal sebelum dijawab, kirim penjelasan privat lewat DM pemilik saat WhatsApp masih terhubung. Bedakan kegagalan provider, penelepon menutup saat setup, nomor yang tidak berwenang, serta gangguan media WhatsApp. Pemohon yang tidak berwenang hanya mendapat penanganan panggilan yang berlaku untuk akses tersebut. |
| Pemilik minta pindah provider saat telepon berjalan | Siapkan tujuan, sinkronkan konteks terbaru, lalu pindah dan laporkan provider aktif setelah siap. | Jika tujuan gagal dan koneksi lama sehat, lanjutkan audio lama dan kabari kegagalan pilihan baru. Jika koneksi lama sudah putus, ikuti alur pemulihan di bawah. |
| Koneksi audio aktif berhenti | Beri kabar singkat saat pemulihan menimbulkan jeda, lalu lanjutkan audio dan percakapan setelah tersambung. | Jika semua opsi habis atau waktu tunggunya di luar batas pemulihan panggilan, putar pemberitahuan singkat dari audio lokal apabila media WhatsApp masih bisa dipakai. Selesaikan playout dalam batas waktu lalu tutup panggilan dengan status yang benar. Kirim penjelasan dan tindakan pemulihan ke DM pemilik bila tersedia. |
| Media atau koneksi WhatsApp ikut putus | Gunakan DM pemilik jika jalur chat masih tersedia. | Catat hasil notifikasi yang belum terkonfirmasi dan tampilkan di `status`. |

**Usulan lanjutan.** Setelah koneksi chat WhatsApp bot pulih, pengiriman ulang
otomatis bisa dipertimbangkan hanya ketika pesan sebelumnya diketahui belum
terkirim. Perilaku ini masih berupa usulan implementasi.

Audio pemberitahuan memakai klip lokal yang dapat diputar melalui jalur PCM
sidecar. Pesan DM memakai pengiriman teks langsung dari akun bot A ke pemilik
yang terverifikasi dalam percakapan yang sedang dipakai. Kedua jalur bekerja
tanpa inferensi, TTS jaringan, atau giliran agen baru. Catat paling banyak
satu pemberitahuan per tahap gangguan dan kanal. Laporkan bukti paling kuat per
kanal, seperti audio dikeluarkan jalur media atau pesan diterima WhatsApp.
Pisahkan pengiriman pemberitahuan dari cleanup koneksi provider.
Pertahankan transkrip yang sudah final. Pekerjaan yang sudah diterima Hermes
tetap punya ID dan status di percakapan yang sama. Saat ganti koneksi, cocokkan
hasil yang datang terlambat dengan pekerjaan asalnya sebelum menampilkan hasil
atau mencoba ulang.

Contoh saat semua provider benar-benar terkena limit

> Semua provider Live yang dipilih sedang mencapai batas pemakaian. Telepon
> akan ditutup. Kamu bisa lanjut lewat chat.

Contoh DM saat sebabnya campuran

> Audio Live tidak tersedia. Gemini sedang kena limit. Codex perlu login
> ulang lewat Codex CLI. Pekerjaan yang sudah diterima tetap bisa dilihat
> di chat ini.

Jika model teks Hermes juga gagal, ubah petunjuk “lanjut lewat chat” menjadi
status yang tersedia. Tentukan anggaran percobaan dan pemulihan dari
pengukuran dering dan jeda audio, lalu catat nilai yang teruji sebelum rilis.

**Status kode kandidat.** Adapter Codex melaporkan kesiapan audio dan kode
kegagalan dasar pada status yang terautentikasi. Pemeriksaan kuota berjalan
sebagai diagnostik di latar belakang. Jika pemulihan audio masih berlangsung
setelah 1,5 detik dengan media panggilan aktif, adapter mengirim satu klip
“Menyambungkan ulang.” ke antrean audio panggilan. Saat pilihan audio habis, adapter
mengirim pemberitahuan deterministik ke sidecar. Sidecar memutar klip lokal
bila tersedia media dan mengirim rincian lewat DM pemilik dengan tanda terima
WhatsApp. Status membedakan penyerahan frame ke jalur media, penerimaan DM
oleh WhatsApp, dan hasil yang belum terkonfirmasi. Untuk panggilan keluar,
sidecar gabungan menunggu audio siap sebelum mengirim tawaran panggilan.
Saat pemilik mengangkat, koneksi audio yang sudah siap melanjutkan sesi Hermes
aktif yang sama. Jika persiapan gagal, sidecar mencatat alasan dan mengirimnya
lewat DM pemilik. Verifikasi dering, audio, dan penerimaan DM pada perangkat
nyata masih terbuka.
[Adapter saat error](spikes/003-whatsapp-call/call_adapter.py)
· [Klip pemberitahuan](spikes/003-whatsapp-call/assets/README.md)
· [Playout dan pengiriman pemilik](spikes/003-whatsapp-call/source/hermes-wa-voice-opus-media-1/src/calls.rs)

### Perilaku normal dan kegagalan untuk fitur lain

| Fitur | Saat berhasil | Saat gagal dan tindakan pemulihan |
| --- | --- | --- |
| Install, setup, enable, dan update | Setup yang valid menyiapkan runtime untuk platform, profil, dan fitur yang dipilih. | Sebut dependensi, checksum, kompatibilitas Hermes, atau persetujuan yang menghalangi langkah tersebut. Pertahankan konfigurasi terakhir yang valid. Laporkan bila plugin terpasang tetapi belum aktif atau layanan belum siap. |
| Disable, uninstall, dan purge | Verifikasi proses berhenti. Pada purge laporkan hasil lokal dan pelepasan perangkat per akun. | Jika proses belum berhenti atau kepemilikan data belum jelas, hentikan penghapusan yang berisiko dan tunjukkan langkah pemulihan. Jika perangkat belum terkonfirmasi lepas, tunjukkan Perangkat Tertaut. Ikuti kontrak lifecycle di atas. |
| Pairing dan multiakun | Kaitkan masing-masing sesi ke identitas yang diverifikasi. Jalankan aksi lewat akun yang dipilih. | QR kedaluwarsa bisa diperbarui. Jika salah nomor atau identitas sudah dipakai, minta pilihan yang benar dan pertahankan pairing yang valid. Bedakan reconnect sementara dengan sesi yang dicabut dan perlu ditautkan lagi. Dalam operasi banyak akun, laporkan akun offline dan lanjutkan akun tujuan lain yang masih memenuhi syarat. |
| Bot A untuk telepon dan akses pemilik | Cocokkan identitas akun, pemilik, dan sesi sebelum mengeksekusi perintah. | Akun tambahan tetap bisa dipakai untuk Control. Tolak telepon pada profil atau akun yang salah. Jika model teks Hermes gagal memvalidasi maksud pemilik, hentikan operasi sebelum efek dan laporkan verifikasi belum tersedia. |
| Kontak, pencarian, dan forwarding | Gunakan akun sumber dan ID kontak atau pesan yang cocok. | Minta pengguna memilih bila hasil ambigu. Jika kontak belum tersinkron atau pesan dan file sumber hilang, laporkan bagian yang perlu diperbaiki. |
| Kirim teks, file, media, dan broadcast | Laporkan hasil per akun dan tujuan sesuai tanda terima yang tersedia. | Bedakan berjalan, ditolak, sebagian berhasil, dan hasil belum pasti. Cocokkan ID pesan dengan ledger sebelum retry. Ulangi hanya tujuan yang diketahui aman. Untuk media yang hilang, berubah, atau ditolak, tunjukkan file yang perlu dikirim ulang. |
| Status WhatsApp | Terapkan format dan audiens yang dimaksud serta catat bukti kirim. | Jika audiens, media, atau dukungan transport belum dapat dipastikan, hentikan publikasi yang belum aman. Verifikasi hasil sebelumnya sebelum mengulang dan pertahankan audiens pilihan. |
| Buat dan atur grup | Gunakan izin admin serta grup dan anggota yang dipilih. Laporkan hasil tiap aksi. | Bedakan grup atau anggota tidak ditemukan, izin tidak berlaku, dan perubahan yang sudah diterapkan sebagian. Periksa metadata grup sebelum mencoba lagi. |
| Chatbot tamu, mention, dan handoff | Ikuti pengaturan tiap akun dan percakapan. Saat handoff, jeda sampai waktunya habis atau pemilik mengaktifkannya kembali. | Jika model chatbot gagal, tampilkan sebab pada status pemilik. Jika chat tamu masih memenuhi aturan dan bisa dikirimi pesan, beri pemberitahuan netral sekali selama gangguan di chat itu. Hormati jeda handoff, isolasi tamu, dan aturan mention. Lanjutkan dari pesan baru setelah pulih. |
| Foto dan voice note chatbot | Gunakan isi media yang berhasil dibaca. Kirim suara jika pembuatan dan pengirimannya berhasil. | Jika STT atau gambar gagal, minta kirim ulang atau teks. Jika TTS gagal dan teks tersedia, balas dengan teks. Jika pengiriman suara belum pasti, periksa tanda terima sebelum fallback teks. Tangani kegagalan kredensial model sesuai penyebabnya. |
| Percakapan Hermes dan pekerjaan aktif | DM dan suara memakai percakapan aktif yang sama. Catat progres dan hasil sesuai ID pekerjaan. | Jika sesi sedang tersambung ulang, tampilkan status pemulihan. Jika pekerjaan gagal masuk, laporkan belum diterima. Jika sudah diterima, ambil status menurut ID dan lanjutkan hasil saat kanal tersedia. |

Control melaporkan gangguan model chatbot per akun di `status` pemilik dengan
kode penyebab yang disanitasi. Setiap chat
yang memenuhi aturan bisa menerima satu pemberitahuan netral selama gangguan.
Respons model yang valid mereset status untuk akun itu. Ledger per operasi
tetap mencegah replay saat hasil pengiriman belum pasti.
[Ledger operasi](whatsapp_control/operations.py)
· [Pemrosesan chatbot](whatsapp_control/chatbot.py)
· [Status pemilik](whatsapp_control/service.py)

## Checklist implementasi dan penerimaan

Centang item setelah mencatat build yang diuji, langkah pengujian, dan hasilnya.
Untuk fitur baru catat status **kode**, **uji lokal**, **uji nyata** jika relevan,
dan temuan yang masih terbuka. Centang kode dan fixture baseline di bawah
mencatat bukti yang sudah ada. Rilis publik mengikuti hasil uji kandidat rilis
dan penutupan regresi yang memblokir rilis.

### Status kandidat

| Fitur | Status kode | Bukti saat ini |
| --- | --- | --- |
| Plugin gabungan dan jalur QR terminal | Baseline tersedia | Uji lifecycle dan pairing sintetis |
| Control pesan, media, Status, grup, dan multiakun | Baseline tersedia | Uji service dan transport lokal |
| Chatbot tamu dan handoff | Baseline tersedia | Uji kebijakan, isolasi, dan media lokal |
| Gangguan model chatbot tamu | Tersedia di kandidat | Uji status aman per akun, pemberitahuan per chat, dan pemulihan |
| Alur bersama DM dan telepon | Baseline tersedia | Satu `LiveCall` dengan Codex dan konektor simulasi. Uji konteks, pergantian sesi, dan delegasi sintetis |
| Lifecycle native Hermes | Baseline diuji | Install, enable, disable, dan remove pada profil Hermes v0.23.1 terisolasi |
| Penghapusan data milik plugin | Tersedia di kandidat | Uji lokal purge aktif dan mati, runtime lama dan gabungan, hasil pelepasan per perangkat, dan perlindungan data profil atau instalasi lain |
| Wizard publik netral | Tersedia di kandidat | Fixture profil baru, konfigurasi Live tanpa Codex, penautan sintetis, dan pemakaian ulang runtime. Build release bersih dan uji perangkat masih terbuka |
| Terminal singkat, slash, dan pemilihan natural | Pilihan provider dan pool tersedia | Tiga jalur memakai operasi yang sama. Tool natural diuji dengan giliran teks dan voice note pemilik, izin operator Telegram, serta kegagalan kandidat |
| Gemini Live | Konektor memakai `LiveCall` | Uji WebSocket lokal untuk audio dua arah, transkrip, dan delegasi. Panggilan nyata masih terbuka |
| GPT-Live dengan API key proyek | Konektor memakai `LiveCall` | Fixture WebSocket menguji audio dua arah, transkrip, interupsi, delegasi Hermes, dan pergantian provider. Panggilan nyata masih terbuka |
| Gateway Live kustom yang kompatibel dengan GPT-Live | Konektor GPT-Live menerima endpoint kustom | Fixture menguji autentikasi, audio, rotasi, pergantian, fallback, dan penolakan redirect |
| Broker Realtime Hermes | Konektor memakai `LiveCall` | Fixture broker, sesi WebRTC lokal dua arah, delegasi Hermes, dan fallback saat login ulang. Uji akun nyata ditunda setelah broker mengembalikan `auth_required` |
| Pool round robin dan pindah audio di tengah telepon | Tersedia di kandidat | Uji rotasi provider dan akun, cutover serta kegagalan kandidat pada satu WebSocket dan sesi Hermes. Panggilan nyata masih terbuka |
| Penyebab kegagalan, notifikasi, dan pemulihan | Implementasi sebagian | Fallback pool, status per akun, klip lokal, dan DM kegagalan tersedia di kandidat. Uji perangkat nyata masih terbuka |
| Uji akun nyata dan rilis | Snapshot pengembangan sudah publik | Uji akun nyata dan penerimaan rilis masih terbuka |

### 1. Pertahankan plugin gabungan

- [x] Satu manifest dan lifecycle plugin menggabungkan kode Control dan Live.
  Bot A memakai Rust. Akun tambahan memakai Baileys. Kode pairing terminal,
  verifikasi identitas, dan chatbot per akun tersedia.
- [x] Jalur Codex dan integrasi sesi Hermes punya pengujian lokal. Baseline
  awal lulus 486 tes Python, 82 tes Rust, dan 5 tes Node. Siklus install,
  enable, disable, dan remove diuji pada profil sementara. Audit lanjutan
  menjalankan ulang 46 tes launcher, lifecycle, dan routing akun pada
  Hermes v0.23.1 dengan profil terisolasi. Jalur setup build saat ini ada di
  [README](README.md#setup-plugin).
- [ ] Verifikasi A dan B tertaut bersamaan. Uji B sebagai pemilik DM saat
  tertaut dan saat tidak tertaut untuk Control. Uji label tambahan, default
  akun, satu/beberapa/semua akun, QR nomor yang salah, serta identitas ganda
  termasuk alias PN/LID. Pastikan panggilan hanya berjalan pada akun bot A.
- [ ] Verifikasi teks, gambar, video, audio, voice note, dan dokumen ke satu
  atau beberapa kontak dan grup. Uji kontak tersimpan, pencarian pesan,
  forwarding sesuai asal pesan, kirim ke kontak yang dipilih atau seluruh
  kontak tersinkron yang diminta, dan pelaporan hasil per tujuan.
- [ ] Verifikasi Status teks, foto, video, dan suara. Periksa audiens yang
  dipilih dan visibilitas pada perangkat penerima. Uji pembuatan grup,
  tambah/keluarkan/promosikan/turunkan anggota, dan pengaturan grup sesuai
  izin WhatsApp.
- [ ] Uji konfirmasi operasi dan retry ketika hasil belum pasti. Cocokkan
  hasil dengan ID WhatsApp yang tepat dan verifikasi satu dampak per operasi.
- [x] Perbaiki pemilihan semua kontak pada operasi multiakun. Kirim pesan atau
  publikasi Status berjalan untuk akun yang daftar penerimanya sudah didapat.
  Laporkan akun yang daftar kontaknya gagal dibaca atau kosong secara terpisah.
  Uji lokal mencakup hasil parsial, pelepasan akun, dan retry tanpa memperluas
  penerima yang sudah disetujui atau mengirim ulang hasil yang sudah berhasil.
- [x] Uji lokal gangguan model chatbot. Periksa status pemilik, satu pemberitahuan
  per chat yang memenuhi aturan, isolasi akun, alias PN/LID, mention, handoff,
  dan reset setelah model pulih. Kegagalan gambar yang berhasil dipulihkan
  dengan respons teks mempertahankan status sehat. Regresi gabungan lulus
  dengan 673 tes Python dan 13 subtes. Fixture purge untuk direktori cron yang
  dialihkan menetapkan izin awal secara eksplisit.
- [ ] Uji chatbot tamu per akun, pilihan DM dan grup mati/allowlist/semua,
  aturan mention, instruksi dan batas riwayat per chat, serta isolasi dari
  alat dan konteks pemilik. Uji foto, gambar yang dikutip, voice note masuk,
  balasan teks/suara, handoff berdasarkan maksud pengirim, jeda yang bisa
  diatur, dan balasan otomatis setelah jeda berakhir atau dilanjutkan manual.
  Uji pemberitahuan saat model tidak tersedia dan balasan setelah model pulih.
- [ ] Uji DM sebelum, saat, dan setelah telepon. Lanjutkan sesi baru serta
  sesi lama. Periksa interupsi, lampiran, transkrip, progres kerja, dan hasil
  pekerjaan Hermes yang selesai saat maupun setelah panggilan.

### 2. Sederhanakan setup dan pemilihan

- [x] Daftarkan `/walive` melalui dispatcher plugin Hermes dan gunakan
  operasi pemilihan yang sama dengan terminal. Uji pemilik WhatsApp,
  operator Telegram yang diizinkan, profil berbeda, dan akses yang ditolak.
- [x] Tambahkan wizard `hermes wa-control-live setup` dengan build sidecar dari
  sumber yang dipin, identitas bot dan pemilik, penautan akun A, panduan per
  label untuk akun tambahan, pilihan Live, serta instruksi memulai layanan.
  Fixture lokal mencakup profil baru, penundaan Live, pemakaian ulang secret,
  penolakan akun yang tidak cocok, dan pemakaian ulang perangkat tertaut.
- [ ] Verifikasi wizard dengan build release dari clone bersih dan penautan
  pada perangkat nyata di profil uji.
- [x] Hubungkan tool permintaan natural ke operasi `use` yang juga dipakai
  terminal dan `/walive`. Fixture menguji pilihan untuk koneksi berikutnya,
  pergantian saat panggilan, penolakan akses tamu, dan izin operator Telegram.
  Uji adapter memeriksa sesi Hermes dan sambungan WhatsApp yang sama serta
  kelanjutan audio lama saat kandidat gagal disiapkan.
- [x] Teruskan model dan suara dari alias yang dipilih ke konektor yang mendukungnya.
  Audit loopback menguji dua alias pada provider yang sama dengan model dan suara
  berbeda. Pergantian memakai `LiveCall`, sambungan WhatsApp, dan sesi Hermes
  yang sama. Setup dan konfigurasi memakai alias untuk menentukan pasangan ini.
- [ ] Verifikasi ketiga jalur pada kandidat rilis dengan model Hermes,
  profil berbeda, dan gateway sibuk. Verifikasi pemilihan kombinasi model dan
  suara yang dikonfigurasi lewat permintaan natural dalam pemakaian nyata.
- [ ] Uji pengguna yang memilih provider selain Codex pada instalasi baru
  tanpa login Codex. Pastikan setiap jalur mengecek prasyarat yang diperlukan
  dan pesan setup menyebut kondisi yang perlu diperbaiki.

### 3. Tambahkan koneksi audio dan routing

- [x] Pisahkan diagnostik kuota dari kesiapan audio Codex. Tampilkan status
  audio siap, login diperlukan, timeout, atau koneksi gagal. Uji ganti
  koneksi audio saat sesi DM berubah sebelum dan sesudah setup, dengan
  sambungan WhatsApp yang sama. Perbaikan diuji dengan fixture lokal.
- [x] Pakai satu `LiveCall` untuk sesi, transkrip, interupsi, dan pekerjaan
  Hermes. Verifikasi konteks DM, satu ID pekerjaan, dan satu hasil lisan
  dengan konektor Codex dan konektor simulasi melalui alur Live yang sama.
- [ ] Hubungkan Gemini Live. Uji pilihan model dan suara, audio dan teks
  masuk, audio keluar, interupsi, dan pekerjaan yang dijalankan Hermes
  melalui panggilan nyata. Konektor dan fixture protokol lokal tersedia.
- [x] Hubungkan GPT-Live ke alur `LiveCall` dan pool yang sama. Fixture
  menguji pilihan model dan suara, audio dua arah, transkrip, interupsi,
  serta client delegation ke sesi Hermes aktif. Uji pindah provider dan
  kelanjutan audio lama saat GPT-Live ditolak oleh server.
- [ ] Verifikasi GPT-Live dengan API key proyek pada panggilan WhatsApp nyata.
  Ukur jeda interupsi dan pergantian audio serta periksa progres dan hasil
  pekerjaan yang kembali ke percakapan aktif.
- [x] Hubungkan login Codex profil Hermes lewat broker publik OpenAI Realtime.
  Fixture menguji token sementara, audio WebRTC dua arah, delegasi ke DM
  aktif, serta pemulihan login dalam panggilan yang sama.
- [ ] Uji akun nyata `hermes-realtime` ditunda sampai login Codex di Hermes
  tersedia. Probe mendapat `auth_required` sebelum token audio terbit. Saat
  uji dilanjutkan, verifikasi penerbitan token, akses model, audio WebRTC dua
  arah ke OpenAI, dan panggilan WhatsApp melalui provider ini.
- [x] Hubungkan endpoint WebSocket kustom yang kompatibel dengan GPT-Live.
  Fixture menguji kredensial per alias, penolakan redirect, rotasi, audio dua
  arah, dan pergantian dalam sesi Hermes yang sama saat gateway siap atau
  menolak koneksi.
- [ ] Verifikasi kompatibilitas dan audio dua arah pada gateway yang dipilih
  untuk panggilan nyata sebelum menyatakan gateway itu didukung pada rilis.
- [x] Uji round robin dengan satu dan beberapa anggota, putaran penuh,
  anggota mati atau dalam jeda, beberapa permintaan pemilihan yang beririsan,
  dan kondisi semua anggota tidak tersedia. Uji 429 dan penundaan yang
  ditetapkan provider. Gunakan 15 alias sintetis sebagai salah satu contoh.
- [x] Uji perpindahan koneksi sintetis dalam satu WebSocket dan sesi Hermes.
  Verifikasi hasil pekerjaan yang sudah diterima hanya dibacakan sekali,
  kandidat gagal menyisakan koneksi sehat, fallback saat limit, dan status
  `all_rate_limited` saat pool habis.
- [x] Perbaiki DM ganda di konteks setelah provider berganti. Cocokkan pesan
  berdasarkan identitas internal Hermes ketika pesan yang sama muncul dalam
  snapshot dan pembaruan. Uji lokal mencakup pembaruan yang tiba saat dan
  sesudah perpindahan, pesan berbeda dengan teks yang sama, dan pembaruan
  pekerjaan dalam panggilan WhatsApp dan sesi Hermes yang sama. Setelah
  perbaikan Control multiakun dan Live ini, 676 tes Python, 15 subtes, dan
  5 tes bridge Baileys lulus.
- [x] Simpan ucapan terakhir yang sudah final ketika panggilan ditutup sebelum
  bot menjawab. Tes memastikan ucapan masuk sekali ke sesi Hermes asal dan
  penutupan tetap menghormati tugas yang sudah dicatat lewat gateway.
- [x] Teruskan transkrip suara yang selesai selama persiapan koneksi pengganti.
  Lewati transkrip yang sudah ada di snapshot lewat identitas pesan atau ID
  pengajuan tugas Hermes. Hanya ID tanpa tanda tangan yang dipakai sebagai
  referensi lokal.
  Uji lokal memeriksa bahwa konteks masuk sebelum koneksi pengganti menerima
  audio, dengan panggilan, sesi Hermes, dan ID pekerjaan yang sama.
- [x] Kirim perintah pengosongan antrean ketika pemilik menyela, termasuk
  setelah provider mengirim transkrip final. Regresi lokal memeriksa jalur
  audio pemilik, pemutaran jawaban berikutnya, sinyal interupsi lama yang
  terlambat, pembatalan salam penutup, dan ID tugas yang sama.
- [ ] Ukur jeda interupsi di WhatsApp nyata dan bandingkan dengan Desktop.
  Pengosongan antrean sidecar tidak menarik kembali frame yang sudah
  diserahkan ke jalur media. Bedakan jawaban lengkap dan terputus sesuai
  bukti yang bisa diukur.
- [ ] Uji maksud penutupan alami ketika tugas berjalan, dengan dan tanpa
  permintaan tugas pada ucapan yang sama. Bedakan penutupan panggilan, permintaan
  berhenti bicara, dan pembatalan tugas. Simulasi penutupan dengan satu maksud
  dan salam yang bisa disela sudah tersedia.
- [x] Perbaiki pemulihan akun yang sama setelah gangguan sementara pada
  panggilan aktif. Fixture mengonfirmasi percobaan ulang terikat pada alias akun
  dan API key yang sama jika digunakan. Koneksi gagal ditutup sebelum sesi
  pengganti, audio lanjut dalam sesi Hermes dan WebSocket WhatsApp yang sama,
  dan notifikasi akhir muncul jika percobaan gagal. Cooldown dan rate limit
  tetap dihormati.
  Fixture GPT-Live mencakup `expired` dengan sesi provider pengganti.
  Regresi kandidat lulus dengan 668 tes Python dan 13 subtes.
- [ ] Uji pindah key pada provider yang sama dan pindah antar-provider, lewat
  perintah langsung dan kegagalan koneksi. Cocokkan ID panggilan WhatsApp dan
  sesi Hermes sebelum dan sesudah. Uji calon gagal saat koneksi lama sehat,
  pemulihan saat koneksi lama putus, event terlambat, transkrip, pekerjaan
  yang sedang berjalan, dan jeda audio. Catat hasil panggilan nyata.
- [x] Uji pemulihan login Codex dengan fixture. Pool melewati Codex selama
  jeda retry dan mengecek login pada percobaan koneksi berikutnya. Setelah
  audio siap, Codex bisa dipilih lagi dalam panggilan dan sesi Hermes yang
  sama. Gemini tetap menunggu penggantian key yang ditolak.
- [ ] Uji klasifikasi dan pemulihan dengan fixture provider. Cakup pembukaan
  sukses, `429` sementara dengan dan tanpa petunjuk retry, kuota dan kredit,
  OAuth perlu refresh atau login ulang, key ditolak, izin dan setup yang salah,
  koneksi putus, durasi sesi habis, dan alasan penutupan tidak diketahui.
  Periksa bahwa alias kembali tersedia setelah validasi kredensial berhasil.
- [x] Uji pemberitahuan pool habis secara lokal dengan kegagalan sebelum
  setup dan saat telepon berlangsung. Periksa klip PCM, teks dengan sebab
  yang berbeda, status pengiriman, dan satu percobaan DM per panggilan.
- [x] Uji lokal pemberitahuan selama pemulihan audio. Setelah media aktif,
  pemulihan yang berjeda memutar satu klip lokal tanpa membuka giliran Hermes.
  Periksa pemulihan cepat, pemulihan sebelum media aktif, kelanjutan audio pada
  panggilan dan sesi yang sama, serta pemberitahuan akhir jika opsi habis.
  Simulasi sidecar memastikan klip masuk ke jalur pemutaran dan media dari
  penelepon tetap masuk. Regresi lulus dengan 680 tes Python, 15 subtes,
  92 tes Rust, dan 5 tes Baileys. Tujuh tes provider nyata tetap ditunda.
- [x] Uji panggilan keluar secara lokal. Pool habis sebelum dering melepas
  slot panggilan tanpa mengirim tawaran. Saat dijawab, audio memakai koneksi
  yang sudah siap dan sesi Hermes yang sama. Penolakan atau provider yang
  putus saat dering menutup koneksi dan melepas slot panggilan.
- [ ] Pada perangkat nyata, uji pool habis sebelum dering dan saat
  panggilan berlangsung. Periksa kejelasan klip saat audio sedang dipulihkan,
  pesan khusus limit dan kegagalan campuran, audio lokal saat provider mati,
  DM saat media putus, serta status ketika DM juga
  gagal. Verifikasi batas pemulihan, penghentian yang bersih, bukti pengiriman,
  transkrip, dan pekerjaan yang sudah diterima.

### 4. Siapkan rilis publik

Gunakan kembali checkout dan dependensi Hermes uji yang cocok untuk regresi.
Buat instalasi sementara saat skenarionya memang menguji lifecycle, lalu
bersihkan hasil tes yang dapat dibuat ulang setelah bukti dicatat. Gunakan
satu direktori `CARGO_TARGET_DIR` per checkout untuk putaran build lokal agar
cache dependensi dan hasil kompilasi tidak bertumpuk di beberapa tempat.
Output build, cache, virtualenv, dan data runtime lokal mengikuti `.gitignore`.

- [ ] Uji instalasi dari clone sumber bersih pada profil Hermes baru dan
  setiap platform/arsitektur yang dinyatakan didukung. Cocokkan artefak dengan
  build dan checksum. Uji setup, scan, perbaikan pairing, dan restart service.
- [ ] Uji install aktif dan nonaktif, enable, disable, serta enable kembali
  melalui CLI Hermes. Saat disable pastikan perintah plugin tidak tersedia,
  proses Control dan Live berhenti, dan pairing bisa dipakai lagi setelah enable.
- [ ] Uji update yang mengikuti sumber Git, pembaruan revisi yang dipin, serta
  uninstall dan install ulang. Cocokkan kode dan konfigurasi yang dihapus oleh
  Hermes dengan pairing lokal yang dipertahankan.
- [x] Implementasikan dan uji lokal purge saat layanan aktif dan saat sudah berhenti.
  Periksa data Control, Live gabungan, dan runtime lama milik instalasi.
  Catat hasil lokal dan pelepasan perangkat untuk setiap akun, termasuk bot
  yang belum terdaftar di Control dan pelepasan yang belum terkonfirmasi.
  Verifikasi riwayat, kredensial bersama, dan pairing bawaan Hermes, serta
  data instalasi dan profil lain sesudah cleanup.
- [ ] Verifikasi hasil pelepasan perangkat dan pembersihan pada instalasi
  rilis dengan akun WhatsApp nyata.
- [ ] Audit isi dan riwayat yang akan menjadi publik. Pisahkan credential,
  state pairing, log, rekaman, dan catatan lokal dari sumber rilis. Panduan
  GitHub menjelaskan platform yang teruji, provider yang tersedia, setup,
  perintah singkat, serta masalah yang masih diketahui.
- [ ] Jalankan kondisi sukses dan gagal dalam matriks BRD menggunakan
  bukti per akun atau tujuan. Uji operasi Control yang sukses sebagian,
  chatbot yang gagal dengan handoff aktif, dan gangguan Live saat Control
  serta chat Hermes masih tersedia.
- [ ] Cocokkan semua skenario penerimaan dengan bukti build rilis, tutup
  regresi penghalang, lalu terbitkan rilis yang lulus penerimaan.
