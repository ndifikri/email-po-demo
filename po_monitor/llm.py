"""Klasifikasi & ekstraksi email PO tenaga kerja dengan LLM (structured output)."""
from __future__ import annotations

from datetime import datetime

import openai
from pydantic import BaseModel, Field, ValidationError

from .models import EmailMessage

# Catatan: field nullable tanpa default supaya kompatibel dengan strict structured output.


class PositionItem(BaseModel):
    title: str | None = Field(description="Nama posisi/jabatan yang dibutuhkan")
    quantity: int | None = Field(description="Jumlah tenaga kerja untuk posisi ini")
    gender: str | None = Field(description="Syarat gender, mis. pria/wanita/bebas")
    age_range: str | None = Field(description="Syarat rentang usia, mis. 20-30 tahun")
    qualifications: str | None = Field(description="Kualifikasi/skill/pendidikan/pengalaman")
    shift_or_hours: str | None = Field(description="Shift atau jam kerja")
    rate_or_budget: str | None = Field(description="Tarif, gaji, atau budget per orang")


class POData(BaseModel):
    client_company: str | None = Field(description="Nama perusahaan pemesan")
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    po_number: str | None = Field(description="Nomor PO jika disebutkan")
    po_date: str | None = Field(description="Tanggal PO, format YYYY-MM-DD jika bisa")
    site_name: str | None = Field(description="Nama site/proyek/cabang penempatan")
    work_location: str | None = Field(description="Lokasi/alamat/kota penempatan")
    start_date: str | None = Field(description="Tanggal mulai kerja, format YYYY-MM-DD jika bisa")
    contract_duration: str | None = Field(description="Durasi kontrak, mis. 6 bulan")
    benefits: str | None = Field(description="Benefit/fasilitas, mis. makan, transport, mess, BPJS")
    notes: str | None = Field(description="Informasi penting lain terkait pesanan")
    positions: list[PositionItem]
    # Sengaja paling akhir: model meringkas setelah field lain diekstrak, jadi isinya konsisten.
    summary: str = Field(description="Ringkasan 1 kalimat permintaan tenaga kerja (Bahasa Indonesia)")


class POAnalysis(BaseModel):
    is_po: bool = Field(description="True jika email adalah PO/permintaan penyediaan tenaga kerja")
    confidence: float = Field(description="Keyakinan klasifikasi 0-1")
    reason: str = Field(description="Alasan singkat klasifikasi (1-2 kalimat, Bahasa Indonesia)")
    po: POData | None = Field(description="Wajib diisi jika is_po true, null jika bukan PO")


SYSTEM_PROMPT = """\
Kamu adalah asisten yang memilah email masuk untuk perusahaan penyedia tenaga kerja (outsourcing/manpower).

Tugas:
1. Tentukan apakah email adalah PO kebutuhan tenaga kerja: email dari klien/calon klien yang memesan atau
   meminta disediakan sejumlah tenaga kerja (pekerja/karyawan outsourcing) untuk posisi tertentu.
   Email bisa berbahasa Indonesia atau Inggris, dan bisa dari domain mana saja.
2. Jika ya, ekstrak detail pesanan ke field `po`. Satu PO bisa berisi beberapa posisi; buat satu item
   `positions` per posisi beserta jumlahnya.

BUKAN PO tenaga kerja (is_po=false, po=null), misalnya:
- lamaran kerja / CV dari kandidat
- purchase order pembelian barang, material, atau jasa non-tenaga-kerja
- iklan lowongan kerja umum, newsletter, promosi, tagihan/invoice, notifikasi sistem
- email internal yang hanya membahas PO lama tanpa pesanan baru

Aturan ekstraksi:
- Isi hanya dengan informasi yang tertulis di email. Jika tidak disebutkan, isi null. Jangan menebak atau mengarang.
- `quantity` harus angka bulat jika jumlah disebutkan.
- Tulis tanggal dalam format YYYY-MM-DD bila tanggalnya jelas; gunakan tanggal terima email sebagai acuan
  untuk tanggal relatif (mis. "senin depan") hanya jika maknanya tegas, selain itu salin teks aslinya.
- `confidence` mencerminkan seberapa yakin kamu bahwa klasifikasi benar (0-1).
- `summary`: satu kalimat Bahasa Indonesia (maksimal ±150 karakter) yang merangkum informasi dasar
  permintaan: posisi beserta jumlahnya, lokasi/site, dan tanggal mulai jika ada. Gunakan hanya informasi
  yang sudah kamu ekstrak di field lain; jangan menambahkan apa pun yang tidak tertulis di email.
"""


class LLMError(Exception):
    pass


def build_user_input(email: EmailMessage, max_chars: int) -> str:
    body = email.body_text or email.snippet or ""
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[...dipotong...]"
    received = datetime.fromtimestamp(email.received_at).strftime("%Y-%m-%d %H:%M")
    attachments = ", ".join(email.attachments) if email.attachments else "-"
    return (
        f"Dari: {email.sender}\n"
        f"Subject: {email.subject}\n"
        f"Diterima: {received}\n"
        f"Lampiran: {attachments}\n"
        f"--- Isi email ---\n{body}"
    )


def analyze_email(email: EmailMessage, client: openai.OpenAI, model: str, max_chars: int) -> POAnalysis:
    """Klasifikasi + ekstraksi dalam satu panggilan LLM. Raise LLMError jika gagal."""
    try:
        response = client.responses.parse(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=build_user_input(email, max_chars),
            text_format=POAnalysis,
        )
    except openai.AuthenticationError as e:
        raise LLMError(f"API key OpenAI ditolak: {e.message}") from e
    except openai.NotFoundError as e:
        raise LLMError(f"Model '{model}' tidak ditemukan/tidak tersedia: {e.message}") from e
    except openai.RateLimitError as e:
        raise LLMError(f"Rate limit/kuota OpenAI: {e.message}") from e
    except openai.APITimeoutError as e:
        raise LLMError("Timeout saat memanggil OpenAI") from e
    except openai.APIConnectionError as e:
        raise LLMError(f"Gagal terhubung ke OpenAI: {e}") from e
    except openai.APIStatusError as e:
        raise LLMError(f"Error OpenAI ({e.status_code}): {e.message}") from e
    except ValidationError as e:
        raise LLMError(f"Respons LLM tidak sesuai skema: {e}") from e

    result = response.output_parsed
    if result is None:
        raise LLMError("LLM tidak mengembalikan output terstruktur (mungkin menolak permintaan)")
    if result.is_po and result.po is None:
        raise LLMError("LLM menandai email sebagai PO tetapi data PO kosong")
    if not result.is_po:
        result.po = None
    result.confidence = min(max(result.confidence, 0.0), 1.0)
    return result
