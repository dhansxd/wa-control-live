# Arah dan aturan pengembangan WA Control & Live

Baca [handoff](HANDOFF.md) saat memulai dari clone atau sesi baru.
Baca [PRD dan checklist](PLAN.md) untuk ruang lingkup dan perilaku yang diminta.
Cocokkan dengan [status kandidat](STATUS.md), kode yang ada, pengujian, dan
riwayat Git yang tersedia sebelum menentukan pekerjaan berikutnya. Implementasi
lanjutan sedang dijeda. Lanjutkan perubahan sesuai tugas yang diminta.

## Arah produk

- Pertahankan WhatsApp Control yang sudah ada sebagai dasar satu plugin
  komunitas. Gabungkan Live ke dalamnya dan jaga kemampuan Control untuk
  akun bot dan akun tambahan.
- Satu penautan akun bot Hermes melayani Control, DM pemilik, dan telepon.
  Hanya akun bot memakai Live. Akun Control tambahan ditautkan sesuai pilihan
  pengguna. Dua akun atau lima belas key dalam contoh adalah skenario uji,
  sehingga angka contoh tidak menjadi batas produk.
- Pakai percakapan WhatsApp Hermes yang sedang aktif untuk DM, audio,
  lampiran, transkrip, dan pekerjaan. Target perilaku pemilik adalah Voice di
  ChatGPT Desktop. Panggilan dari sesi baru, sesi yang dilanjutkan, dan perubahan
  sesi saat telepon mengikuti sesi aktif yang sama.
- Pakai satu alur percakapan Live. Pemilihan provider, model, akun atau key,
  serta pool menentukan koneksi audio. Pergantian audio tetap mengikuti
  percakapan dan pekerjaan Hermes yang sedang berjalan.
- Pertahankan isolasi chatbot tamu per akun dan chat. Model tamu membaca
  instruksi, pesan masuk, dan riwayat chat sesuai konfigurasi, tanpa akses alat
  atau konteks pribadi pemilik. Handoff mengikuti maksud pengirim dalam bahasa
  natural dan durasinya bisa diatur.
- Utamakan pengaturan harian melalui permintaan natural ke Hermes atau slash
  command yang relevan. Setup awal dan penautan dari terminal dapat dipakai
  pada profil Hermes baru dengan kredensial dan provider pilihan pengguna.
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
   mengikuti bukti dari pengujian kode, simulasi, atau pemakaian nyata.
5. Publikasikan sumber, dokumentasi, tes sintetis, dan atribusi. Simpan
   kredensial, data pairing, nomor nyata, rekaman pribadi, dan profil Hermes
   di luar repo publik.
