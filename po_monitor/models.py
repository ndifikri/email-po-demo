"""Model data bersama, tanpa ketergantungan ke Gmail/OpenAI/Streamlit."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EmailMessage:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    received_at: int  # epoch detik
    snippet: str
    body_text: str
    attachments: list[str] = field(default_factory=list)
