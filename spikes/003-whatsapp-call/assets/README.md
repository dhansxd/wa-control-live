# Klip pemberitahuan Live

Klip memuat teks pemberitahuan asli yang disintesis dengan suara formant
Bahasa Indonesia standar `id` dari eSpeak NG 1.52.0. FFmpeg 9.0.2 mengubah
keluaran 22.050 Hz menjadi PCM mentah mono 16.000 Hz bertanda 16-bit
little-endian. Plugin memutar berkas PCM yang sudah tersedia saat pemulihan
audio menimbulkan jeda atau gagal. [Suara Bahasa Indonesia eSpeak NG](https://github.com/espeak-ng/espeak-ng/blob/1.52.0/docs/languages.md) · [Lisensi alat dan keluaran sintetis](https://www.gnu.org/licenses/gpl-faq.html#WhatCaseIsOutputGPL)

| Klip | Teks | Durasi |
| --- | --- | --- |
| `live-unavailable.pcm` | Layanan suara sedang tidak tersedia. | 2,360 detik |
| `live-rate-limited.pcm` | Batas pemakaian layanan suara tercapai. | 2,499 detik |
| `live-recovering.pcm` | Menyambungkan ulang. | 1,129 detik |

Untuk membuat ulang klip di direktori ini, jalankan eSpeak NG dengan seed
deterministik bawaan `-D` dan resample WAV dari stdout.

```sh
espeak-ng -D -v id -s 170 -z -b 1 --stdout 'Layanan suara sedang tidak tersedia.' |
  ffmpeg -hide_banner -loglevel error -i pipe:0 -ar 16000 -ac 1 -f s16le live-unavailable.pcm
espeak-ng -D -v id -s 170 -z -b 1 --stdout 'Batas pemakaian layanan suara tercapai.' |
  ffmpeg -hide_banner -loglevel error -i pipe:0 -ar 16000 -ac 1 -f s16le live-rate-limited.pcm
espeak-ng -D -v id -s 170 -z -b 1 --stdout 'Menyambungkan ulang.' |
  ffmpeg -hide_banner -loglevel error -i pipe:0 -ar 16000 -ac 1 -f s16le live-recovering.pcm
```
