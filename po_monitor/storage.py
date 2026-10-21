"""Penyimpanan SQLite untuk email, hasil klasifikasi, dan ekstraksi PO."""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from .llm import POAnalysis, POData
from .models import EmailMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    message_id    TEXT PRIMARY KEY,
    thread_id     TEXT,
    sender        TEXT,
    subject       TEXT,
    received_at   INTEGER NOT NULL,
    snippet       TEXT,
    body_text     TEXT,
    attachments   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    processed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);

CREATE TABLE IF NOT EXISTS classifications (
    message_id   TEXT PRIMARY KEY REFERENCES emails(message_id) ON DELETE CASCADE,
    is_po        INTEGER NOT NULL,
    confidence   REAL NOT NULL,
    reason       TEXT,
    needs_review INTEGER NOT NULL,
    model        TEXT,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS po_extractions (
    message_id      TEXT PRIMARY KEY REFERENCES emails(message_id) ON DELETE CASCADE,
    data_json       TEXT NOT NULL,
    po_number       TEXT,
    client_company  TEXT,
    total_headcount INTEGER NOT NULL DEFAULT 0
);
"""

FILTERS = ("all", "po", "non_po", "review", "error")

_FILTER_SQL = {
    "all": "1=1",
    "po": "e.status = 'processed' AND c.is_po = 1",
    "non_po": "e.status = 'processed' AND c.is_po = 0",
    "review": "e.status = 'processed' AND c.needs_review = 1",
    "error": "e.status = 'error'",
}


def total_headcount(po: POData | None) -> int:
    if po is None:
        return 0
    return sum(p.quantity or 0 for p in po.positions)


def format_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


class Storage:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # timeout: listener (thread background) dan dashboard menulis/membaca bersamaan.
        with closing(sqlite3.connect(self.db_path, timeout=15)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn

    # --- Email ---------------------------------------------------------------

    def existing_message_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        if not ids:
            return set()
        found: set[str] = set()
        with self._conn() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                rows = conn.execute(
                    f"SELECT message_id FROM emails WHERE message_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                found.update(r["message_id"] for r in rows)
        return found

    def data_version(self) -> tuple:
        """Penanda murah untuk mendeteksi perubahan data (dipakai refresh dashboard)."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*), COALESCE(MAX(processed_at), 0),
                          COALESCE(SUM(status = 'pending'), 0), COALESCE(SUM(status = 'error'), 0)
                   FROM emails"""
            ).fetchone()
        return tuple(row)

    def pending_message_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT message_id FROM emails WHERE status = 'pending' ORDER BY received_at"
            ).fetchall()
        return [r["message_id"] for r in rows]

    def save_email(self, email: EmailMessage) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO emails
                   (message_id, thread_id, sender, subject, received_at, snippet, body_text, attachments, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    email.message_id,
                    email.thread_id,
                    email.sender,
                    email.subject,
                    email.received_at,
                    email.snippet,
                    email.body_text,
                    json.dumps(email.attachments),
                ),
            )

    def get_email(self, message_id: str) -> EmailMessage | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM emails WHERE message_id = ?", (message_id,)).fetchone()
        if row is None:
            return None
        return EmailMessage(
            message_id=row["message_id"],
            thread_id=row["thread_id"],
            sender=row["sender"],
            subject=row["subject"],
            received_at=row["received_at"],
            snippet=row["snippet"],
            body_text=row["body_text"],
            attachments=json.loads(row["attachments"] or "[]"),
        )

    # --- Hasil analisis --------------------------------------------------------

    def save_result(self, message_id: str, analysis: POAnalysis, model: str, threshold: float) -> None:
        """Simpan klasifikasi + ekstraksi dalam satu transaksi, menimpa hasil sebelumnya."""
        now = int(time.time())
        needs_review = analysis.is_po and analysis.confidence < threshold
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO classifications
                   (message_id, is_po, confidence, reason, needs_review, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (message_id, int(analysis.is_po), analysis.confidence, analysis.reason, int(needs_review), model, now),
            )
            conn.execute("DELETE FROM po_extractions WHERE message_id = ?", (message_id,))
            if analysis.is_po and analysis.po is not None:
                po = analysis.po
                conn.execute(
                    """INSERT INTO po_extractions
                       (message_id, data_json, po_number, client_company, total_headcount)
                       VALUES (?, ?, ?, ?, ?)""",
                    (message_id, po.model_dump_json(), po.po_number, po.client_company, total_headcount(po)),
                )
            conn.execute(
                "UPDATE emails SET status = 'processed', error_message = NULL, processed_at = ? WHERE message_id = ?",
                (now, message_id),
            )

    def mark_error(self, message_id: str, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE emails SET status = 'error', error_message = ?, processed_at = ? WHERE message_id = ?",
                (message, int(time.time()), message_id),
            )

    # --- Query untuk dashboard -----------------------------------------------

    def _where(self, filter_: str, search: str) -> tuple[str, list]:
        if filter_ not in _FILTER_SQL:
            raise ValueError(f"Filter tidak dikenal: {filter_}")
        clauses = [_FILTER_SQL[filter_]]
        params: list = []
        if search:
            clauses.append("(e.sender LIKE ? OR e.subject LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        return " AND ".join(clauses), params

    def list_emails(self, filter_: str = "all", search: str = "") -> list[dict]:
        where, params = self._where(filter_, search)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT e.message_id, e.received_at, e.sender, e.subject, e.status, e.error_message,
                           c.is_po, c.confidence, c.needs_review,
                           p.client_company, p.po_number, p.total_headcount,
                           json_extract(p.data_json, '$.summary') AS summary
                    FROM emails e
                    LEFT JOIN classifications c ON c.message_id = e.message_id
                    LEFT JOIN po_extractions p ON p.message_id = e.message_id
                    WHERE {where}
                    ORDER BY e.received_at DESC""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_email_detail(self, message_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT e.*, c.is_po, c.confidence, c.reason, c.needs_review, c.model, p.data_json
                   FROM emails e
                   LEFT JOIN classifications c ON c.message_id = e.message_id
                   LEFT JOIN po_extractions p ON p.message_id = e.message_id
                   WHERE e.message_id = ?""",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        detail = dict(row)
        detail["attachments"] = json.loads(detail["attachments"] or "[]")
        detail["po"] = POData.model_validate_json(detail.pop("data_json")) if row["data_json"] else None
        return detail

    def metrics(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(e.status = 'processed' AND c.is_po = 1), 0) AS po,
                          COALESCE(SUM(e.status = 'processed' AND c.needs_review = 1), 0) AS review,
                          COALESCE(SUM(e.status = 'error'), 0) AS error,
                          COALESCE(SUM(p.total_headcount), 0) AS headcount
                   FROM emails e
                   LEFT JOIN classifications c ON c.message_id = e.message_id
                   LEFT JOIN po_extractions p ON p.message_id = e.message_id"""
            ).fetchone()
        return dict(row)
