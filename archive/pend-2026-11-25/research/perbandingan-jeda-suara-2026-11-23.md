# Perbandingan jeda suara WhatsApp Live

23 November 2026 · macOS 27.2.1 arm64 · Codex CLI 0.171.0 · plugin cfbb3f147c7a16bec387dff555e432dfb2eb9c1e.

Waktu dihitung sejak akhir klip ucapan pemilik hingga sampel suara jawaban pertama yang melewati amplitudo 500 pada PCM16. Kedua jalur memakai Codex Realtime v3 melalui WebRTC dengan suara Juniper dan rekaman sintetis Damayanti yang sama. Jalur langsung menggunakan app-server Codex. Jalur plugin memakai LiveCall dari revisi yang terpasang, riwayat sintetis dan layanan tugas uji di memori.

| Skenario | Codex langsung | Plugin | Tugas diterima layanan uji |
| --- | ---: | ---: | ---: |
| Permintaan foto langsung | 2,37 dtk | 3,49 dtk | 1 |
| Sapaan, pengantar tugas, cek sambungan, lalu permintaan foto | 2,51 dtk | 3,61 dtk | 1 |

Di kedua skenario, transkrip mengandung permintaan foto dan respons suara muncul sebelum ada pertanyaan susulan. Pada pemeriksaan pendahuluan dengan ambang pengukuran berbeda, Codex juga menjawab pada kedua jalur. Angka di tabel berasal dari run dengan ambang yang sama.

Panggilan WhatsApp nyata mencatat jeda sekitar 30 detik sebelum pemilik kembali bicara. Perbandingan sintetis ini belum mereproduksi jeda tersebut. Perangkat WhatsApp, riwayat pribadi, eksekusi pembuatan gambar dan pemulihan sambungan audio berada di luar pengukuran.

SHA-256 masukan PCM16 mono 16 kHz per skenario, sesuai urutan tabel.

- `0c755807feab52e0250add7ae5c2bb9d23c5572d1430a6fd25a46c055d66447f`
- `f70ab01582aab532c6bc64df9660280047a7c3ddd91f9cde9f46ac7b206eec8c`

## Pemutaran ulang potongan panggilan asli

Potongan berdurasi 37 detik dan berakhir sebelum pertanyaan susulan. Hanya suara masuk dari potongan itu yang digunakan. Riwayat pribadi dan audio lain dari telepon tidak digunakan untuk mengisi konteks uji. Perbandingan memakai revisi, akun provider, versi Realtime, dan suara yang sama dengan uji di atas.

Waktu pada tabel ini dihitung dari posisi klip yang sesuai dengan saat kata terakhir permintaan foto tercatat di log panggilan asli. Titik acuannya 11,383 detik setelah awal potongan. Angka pada tabel sintetis di atas memakai akhir klip sehingga kedua tabel memiliki titik ukur yang berbeda.

| Skenario pemutaran ulang | Audio pertama Codex langsung | Audio pertama plugin | Permintaan tugas diterima layanan uji |
| --- | ---: | ---: | ---: |
| Potongan asli sejak awal sesi uji | 3,33 dtk | 4,77 dtk | 1 |
| Tiga ucapan sintetis, lalu potongan asli | — | 5,69 dtk | 0 |

Pada uji plugin setelah tiga ucapan sintetis, transkrip memuat permintaan foto dan jawaban lisan yang menyatakan akan mengerjakannya. Provider tidak mengirim peristiwa `delegation.created`; penerima tugas uji juga menghitung nol pemanggilan. Pada panggilan asli terdapat satu peristiwa tersebut sesudah pemilik kembali bicara. Hasil ini membedakan kejadian pada panggilan asli dari celah yang ditemukan melalui pemutaran ulang.

Dalam panggilan asli, selama jeda 29 detik amplitudo absolut audio keluaran sebelum penyaringan plugin mencapai maksimum 1 pada PCM16. Sidecar menerima frame audio dari adapter sepanjang jeda itu. Log yang tersedia tidak memuat alasan internal provider menahan giliran.

Dua percobaan pembanding dengan pembaca berkas panjang berhenti karena batas waktu sebelum seluruh masukan selesai. Untuk pembanding akhir, audio dipasok dengan irama 20 ms seperti adapter terpasang dan opsi percakapan default Codex. Pengukuran waktu keluaran mendeteksi sampel dengan amplitudo PCM16 lebih dari 500. Pada jalur plugin, penerima uji mencatat permintaan tugas di memori. Tidak ada gambar yang dibuat atau pesan yang dikirim ke WhatsApp.

Pada uji Codex langsung setelah percakapan pendahulu, pengamatan berlangsung 47,63 dtk sejak titik acuan tanpa keluaran suara melampaui ambang. Status galat transport tercatat nihil.

## Regresi penerimaan tugas setelah jawaban suara langsung

Pengujian terisolasi memakai macOS 27.2.1 arm64, Python 3.12.15, pytest 9.2.0, dan PyAV 16.1.0. Fixture lokal memberikan PCM sintetis, transkrip pemilik yang meminta foto, dan transkrip balasan yang menjanjikan foto. Provider simulasi melewatkan peristiwa delegasi. Penerima tugas uji mencatat sesi, permintaan, dan ID setiap pengajuan. Pada kedua urutan kedatangan transkrip final, adapter menyimpan obrolan dan mencatat nol pengajuan tugas.

| Skenario dengan masukan identik | Commit pembanding | Commit terpasang | Hasil |
| --- | --- | --- | --- |
| Transkrip final pemilik lebih dulu, lalu balasan Live | `6446763` | `cfbb3f1` | Gagal pada keduanya. Diharapkan satu tugas, tercatat nol. |
| Balasan Live lebih dulu, lalu transkrip final pemilik | `6446763` | `cfbb3f1` | Gagal pada keduanya dengan hasil yang sama. |
| Obrolan biasa, permintaan hipotetis, larangan mengerjakan, tanpa suara pemilik, atau tanpa transkrip final pemilik | `6446763` | `cfbb3f1` | Lima kontrol lulus pada keduanya. |
| Skenario lama untuk penerimaan tugas dan penyimpanan percakapan | `6446763` | `cfbb3f1` | Delapan kasus lulus pada keduanya. |
| Sesi, DM dan indikator mengetik, Control, otorisasi, pengajuan tunggal, interupsi dan progres | `907e804` | `cfbb3f1` | 55 kasus dari snapshot tes yang sama lulus pada masing-masing commit. |

Hasil ini mereproduksi kegagalan penerimaan tugas secara lokal. Peristiwa delegasi muncul terlambat pada panggilan asli, sedangkan pada salah satu pemutaran ulang peristiwa itu sama sekali tidak muncul. Kedua kejadian ini perlu dibedakan saat menilai perbaikan.

## Pembanding klien Desktop di Mac

Inspeksi statis pada ChatGPT Desktop versi `26.1117.41037`, build `29803`, menemukan pemanggilan `thread/realtime/start` dengan `threadId` di JavaScript klien yang dikirim dalam `/Applications/ChatGPT.app/Contents/Resources/app.asar`. Salah satu jalur membentuk parameter sesi di `webview/assets/async/191628.7c4ce6e8ec.js`. Temuan ini memeriksa kemampuan kode klien. Jalur yang betul-betul dipakai pada satu panggilan Desktop belum diukur.

Pada plugin `cfbb3f1`, `CodexConnection.prepare` membuka thread Codex sementara untuk audio. `CodexConnection.start` memulai Realtime pada thread itu. Plugin memilih `clientManagedHandoffs` dan mengirim pekerjaan yang diterima melalui sesi WhatsApp Hermes aktif. ID thread audio dan ID sesi Hermes berasal dari dua runtime yang berbeda. Bukti bahwa sesi Hermes mengerjakan suatu permintaan ialah tanda terima tugas pada sesi WhatsApp tersebut.

[Panduan suara ChatGPT Desktop](https://learn.chatgpt.com/docs/features/voice) · [Penjelasan arsitektur GPT-Live oleh OpenAI](https://openai.com/index/continuous-voice-interaction-with-gpt-live/) · [Contoh async tools LiveKit](https://docs.livekit.io/agents/logic/tools/async/) · [Definisi progres dan hasil Pipecat](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/frames/frames.py)

## Uji satu parameter pada pembukaan suara

Pada 23 November 2026, Codex CLI 0.171.0 dijalankan dengan profil sementara dan server HTTP loopback. Kedua kondisi memakai parameter konektor dari `cfbb3f1`, SDP sintetis, opsi delegasi yang sama, dan item developer penanda yang sama. Satu kondisi menyertakan `prompt` penanda, panjang 24 karakter. Pada kondisi tersebut penanda itu menjadi seluruh nilai `instructions` untuk suara. Pada kondisi yang menghilangkan hanya `prompt`, Codex mengirim instruksi suara bawaannya. Item developer dan opsi delegasi tetap hadir pada kedua kondisi. Pengukuran berhenti sesudah server lokal menerima konfigurasi pembukaan, sebelum panggilan audio.

Eksperimen ini menunjukkan instruksi dasar dapat dipertahankan sambil tetap memasok konteks Hermes. Pengujian percakapan masih diperlukan untuk mengukur dampak terhadap delegasi, identitas Hermes, jawaban ganda, dan jeda. Pembandingan Desktop sebaiknya memakai tugas yang dibuka dari chat yang sudah memiliki satu detail unik, serta alat dengan tahapan dan hasil yang bisa diperiksa pada kedua permukaan.
