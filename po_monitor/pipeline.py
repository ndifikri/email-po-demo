"""Pemrosesan per email: ambil → simpan pending → analisis LLM → simpan hasil."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .config import AppConfig
from .llm import POAnalysis
from .models import EmailMessage
from .storage import Storage

Analyzer = Callable[[EmailMessage], POAnalysis]


class MailSource(Protocol):
    def current_history_id(self) -> str: ...
    def list_added_since(self, history_id: str) -> tuple[list[str], str]: ...
    def fetch_message(self, message_id: str) -> EmailMessage: ...


def process_email(store: Storage, analyze: Analyzer, cfg: AppConfig, email: EmailMessage) -> POAnalysis:
    """Analisis satu email dan simpan hasilnya; error ditandai di DB lalu di-raise ulang."""
    try:
        analysis = analyze(email)
    except Exception as e:
        store.mark_error(email.message_id, str(e))
        raise
    store.save_result(email.message_id, analysis, cfg.model, cfg.confidence_threshold)
    return analysis


def ingest(store: Storage, source: MailSource, analyze: Analyzer, cfg: AppConfig, message_id: str) -> POAnalysis:
    """Ambil email baru dari Gmail, simpan sebagai pending, lalu analisis.

    Error saat fetch di-raise tanpa menyimpan apa pun (email bisa dicoba lagi);
    error saat analisis sudah tercatat di DB sebelum di-raise.
    """
    email = source.fetch_message(message_id)
    store.save_email(email)
    return process_email(store, analyze, cfg, email)


def reprocess(store: Storage, analyze: Analyzer, cfg: AppConfig, message_id: str) -> POAnalysis:
    """Proses ulang email yang sudah tersimpan (memakai isi email dari DB)."""
    email = store.get_email(message_id)
    if email is None:
        raise KeyError(message_id)
    return process_email(store, analyze, cfg, email)
