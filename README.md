# Monitor PO Tenaga Kerja

Aplikasi Streamlit yang memantau email baru di inbox Gmail, lalu memakai LLM OpenAI (`gpt-6-luna`) untuk:

1. **Mengklasifikasi** apakah email merupakan **PO (pemesanan) kebutuhan tenaga kerja** dari klien, beserta confidence dan alasannya.
2. **Mengekstrak** detail pesanan: perusahaan pemesan, kontak, nomor PO, site/lokasi, tanggal mulai, durasi kontrak, benefit, dan daftar posisi (jumlah, gender, usia, kualifikasi, shift, tarif).
3. **Meringkas** permintaan dalam satu kalimat (kolom **Detail**), misalnya *"8 Security dan 4 Cleaning Service untuk site Cikarang, mulai 2026-11-01"*.

Hasilnya tampil di dashboard dengan filter, panel detail, tombol proses ulang, dan export CSV.

## Cara kerja

```
Gmail (History API) ──▶ listener (thread background, cek tiap 15 detik)
                              │
                              ▼
                     LLM: klasifikasi + ekstraksi + ringkasan (1 panggilan)
                              │
                              ▼
                     SQLite (data/po_monitor.db) ──▶ dashboard Streamlit
```

- Hanya email yang masuk **selama aplikasi nyala** yang diproses. Email lama di inbox, serta email yang masuk saat aplikasi mati atau tidur, diabaikan.
- Satu listener per proses aplikasi, berapa pun tab yang terbuka. Dashboard refresh otomatis sekitar 10 detik sekali.
- Akses Gmail hanya baca (`gmail.readonly`). Isi lampiran tidak dibaca, hanya namanya yang dicatat.

## Struktur

```
app.py                       # UI Streamlit (entry point)
po_monitor/
  config.py                  # baca & validasi st.secrets
  gmail_client.py            # OAuth Google, token, baca & parsing email
  llm.py                     # skema Pydantic + prompt + panggilan OpenAI
  storage.py                 # SQLite
  pipeline.py                # per email: ambil → simpan → analisis → simpan hasil
  listener.py                # thread background + deteksi sleep
tests/                       # pytest (Gmail & LLM di-mock)
.streamlit/secrets.toml.example
requirements.txt             # dependensi runtime
requirements-dev.txt         # + pytest
```

## Secrets

Semua secret dibaca dari `st.secrets`. Formatnya ada di [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example):

| Bagian | Key | Keterangan |
|---|---|---|
| `[openai]` | `api_key` | wajib |
| | `model` | default `gpt-6-luna` |
| `[google_oauth]` | `client_id`, `client_secret` | wajib, dari OAuth client tipe *Web application* |
| | `project_id` | ID project Google Cloud |
| | `redirect_uri` | URL aplikasi, harus sama persis dengan *Authorized redirect URI* di Google Cloud |
| `[app]` | `poll_interval_seconds`, `max_emails_per_cycle`, `confidence_threshold`, `max_body_chars` | opsional |

`secrets.toml`, token Gmail (`.secrets/`), dan database (`data/`) sudah di-`.gitignore`. Jangan pernah meng-commit file tersebut.

## Menjalankan di lokal

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # lalu isi nilainya
streamlit run app.py
```

Buka `http://localhost:8501`, klik **Login dengan Google**, dan izinkan akses baca Gmail. Untuk lokal, `redirect_uri = "http://localhost:8501"`.

Menjalankan test:

```bash
pytest -q
```

## Deploy ke Streamlit Community Cloud

1. **Push repo ini ke GitHub.** Pastikan `secrets.toml` tidak ikut ter-commit.
2. Di [share.streamlit.io](https://share.streamlit.io), pilih **Create app**, lalu isi repo, branch, dan *Main file path* `app.py`.
   Di **Advanced settings**, pilih Python **3.14** (versi yang dipakai saat pengembangan).
3. Masih di **Advanced settings → Secrets**, tempel isi `secrets.toml` Anda dengan
   `redirect_uri = "https://<nama-app>.streamlit.app"` (tanpa garis miring di akhir).
   Secrets bisa diubah nanti lewat **Settings → Secrets**.
4. **Google Cloud Console** (APIs & Services):
   - Aktifkan **Gmail API**.
   - Pada OAuth client, tambahkan **Authorized redirect URI** `https://<nama-app>.streamlit.app`. URI localhost boleh tetap ada.
   - Jika OAuth consent screen masih mode *Testing*, tambahkan alamat Gmail Anda sebagai *Test user*.
     Di mode ini refresh token kadaluarsa tiap 7 hari, jadi aplikasi akan meminta login ulang.
5. Buka URL aplikasi, lalu login dengan Google.

### Penting sebelum hosting

- **Aplikasi ini single-user.** Token Gmail disimpan satu untuk seluruh aplikasi, bukan per pengunjung. Siapa pun yang membuka URL akan melihat dashboard inbox yang sedang login, dan bisa logout atau login dengan akun lain.
  **Jadikan app private.** Di **Settings → Sharing**, batasi viewer hanya ke email yang diizinkan.
- **Penyimpanan tidak permanen.** Filesystem Streamlit Cloud bersifat sementara. Database SQLite dan token Gmail hilang saat app di-reboot, di-redeploy, atau tidur. Setelah itu riwayat kosong lagi dan Anda perlu login ulang.
- **Listener hanya jalan selama app aktif.** App yang lama tidak dikunjungi akan ditidurkan oleh Streamlit Cloud. Email yang masuk selama itu tidak diproses, dan listener baru jalan lagi setelah halaman dibuka.
- Setiap email baru dikirim ke OpenAI, jadi pemakaian API mengikuti volume email masuk.

Untuk demo, batasan di atas tidak masalah. Untuk pemakaian rutin, simpan data di database eksternal dan jalankan listener sebagai proses terpisah yang selalu menyala.

## Fitur dashboard

- **Metrik**: jumlah email diproses, email PO, perlu review (confidence < ambang), error, dan total tenaga kerja.
- **Tabel**: Tanggal, Pengirim, Subject, Status, Confidence, Perusahaan, Total TK, Detail. Tersedia filter (Semua / Hanya PO / Bukan PO / Perlu review / Error) dan pencarian pengirim/subject.
- **Panel detail**: hasil klasifikasi, ringkasan, data PO dan tabel posisi, isi email asli, serta tombol **Proses ulang**.
- **Download CSV**: kolom dan baris sama dengan tabel sesuai filter aktif, dengan status ditulis tanpa emoji.
