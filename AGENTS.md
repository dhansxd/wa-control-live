# Arah dan aturan pengembangan WA Control & Live

Baca [handoff](HANDOFF.md) saat memulai dari clone atau sesi baru.
Baca [PRD dan checklist](PLAN.md) untuk ruang lingkup dan perilaku yang diminta.
Cocokkan dengan [status kandidat](STATUS.md), kode yang ada, pengujian, dan
riwayat Git yang tersedia sebelum menentukan pekerjaan berikutnya. Lanjutkan
perubahan sesuai tugas yang diminta. Jika aturan lama bertentangan dengan
arahan terbaru pemilik, perbarui aturan dan kriteria penerimaannya.

## Arah produk

- Pertahankan dua kemampuan dalam satu plugin komunitas. WhatsApp Control
  memberi agen Hermes akses ke akun yang ditautkan untuk membaca percakapan,
  membalas atau mengirim pesan dan media, mengelola grup, serta memublikasikan
  Status sesuai izin akun dan permintaan pemilik. WhatsApp Live memungkinkan
  pemilik menelepon Hermes di nomor bot. Chatbot tamu tetap opsional dan
  terpisah dari kendali pemilik.
- Satu penautan akun bot Hermes melayani Control, DM pemilik, dan telepon.
  Deteksi identitas bot dari pairing WhatsApp Hermes yang ada dan cocokkan
  dengan hasil scan Live. Ambil identitas akun Control tambahan dari scan
  QR. Tolak akun bot pada QR Control tambahan. Izin menelepon dan DM pemilik
  dipilih secara terpisah dari identitas akun Control yang ditautkan.
  Dua akun atau lima belas key dalam contoh adalah skenario uji, sehingga
  angka contoh tidak menjadi batas produk.
- Pakai percakapan WhatsApp Hermes yang sedang aktif untuk DM, audio,
  lampiran, transkrip, dan pekerjaan. Cocokkan [perilaku yang bisa dialami
  pemilik di ChatGPT Live Desktop](PLAN.md#acuan-perilaku-telepon) dalam
  skenario penerimaan. Pemilik bisa mengobrol santai saat Hermes bekerja,
  menyela jawaban suara, dan mengakhiri telepon lewat percakapan. Interupsi
  suara dan penutupan telepon membiarkan pekerjaan yang sudah diterima tetap
  berjalan. Perubahan atau pembatalan tugas mengikuti maksud pemilik dan
  kontrol asli Hermes. Penutupan oleh asisten adalah syarat pemilik.
- Pakai satu alur percakapan Live. Pemilihan provider, model, akun atau key,
  serta pool menentukan koneksi audio. Pergantian audio tetap mengikuti
  percakapan dan pekerjaan Hermes yang sedang berjalan.
- Pertahankan isolasi chatbot tamu per akun dan chat. Model tamu membaca
  instruksi, pesan masuk, dan riwayat chat sesuai konfigurasi, tanpa akses alat
  atau konteks pribadi pemilik. Handoff mengikuti maksud pengirim dalam bahasa
  natural dan durasinya bisa diatur.
- Utamakan pengaturan harian melalui permintaan natural ke Hermes atau slash
  command yang relevan. Pasang plugin di profil Hermes yang sudah menjalankan
  bot dan menyimpan percakapannya. Setelah instalasi GitHub dan persetujuan
  dependensi asli Hermes, satu wizard menautkan bot dan menawarkan akun
  Control tambahan. Gunakan lifecycle asli Hermes untuk mengelola plugin
  dan memuat ulang gateway yang sudah berjalan.
- Ikuti lifecycle asli Hermes dan pakai API publik jika tersedia. Untuk
  integrasi sesi yang membutuhkan API internal, batasi pemakaiannya pada
  bagian tersebut, dokumentasikan, dan uji pada versi Hermes yang didukung.

## Cara melanjutkan pekerjaan

1. Verifikasi status terhadap kode, tes, dan commit, karena checklist dapat
   tertinggal. Untuk regresi, cocokkan perilaku kode saat ini dengan revisi
   sebelumnya jika tersedia.
2. Gunakan kembali jalur yang sudah berjalan. Perbaiki celah yang terbukti
   dengan perubahan yang diperlukan, tanpa mengulang implementasi yang sudah
   ada. Fitur baru, pembatasan produk, atau perubahan arah mengikuti ruang
   lingkup yang diminta. Catat ide tambahan sebagai usulan.
3. Uji alur sukses dan kegagalan yang terdampak. Dahulukan implementasi, audit,
   dan regresi lokal. Penautan dan pengujian di perangkat nyata dilakukan pada
   tahap penerimaan setelah kandidat kode siap. Pakai ulang checkout dan
   dependensi uji yang cocok, lalu bersihkan hasil build dan fixture sementara
   yang dapat dibuat ulang.
4. Perbarui PRD dan status setelah setiap perubahan. Catat perilaku yang
   berubah, revisi yang diuji, hasil tes, serta pekerjaan tersisa. Klaim verifikasi
   mengikuti bukti dari pengujian kode, simulasi, atau pemakaian nyata. Untuk
   mengklaim kesetaraan telepon, bandingkan skenario yang sama di ChatGPT Live
   Desktop dan WhatsApp. Tandai detail yang belum terdokumentasi atau diamati
   sebelum menjadikannya aturan.
5. Publikasikan sumber, dokumentasi, tes sintetis, dan atribusi. Simpan
   kredensial, data pairing, nomor nyata, rekaman pribadi, dan profil Hermes
   di luar repo publik.
