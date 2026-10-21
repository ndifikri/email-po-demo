"""Akses Gmail: OAuth web flow, penyimpanan token, dan pembacaan email (read-only)."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from bs4 import BeautifulSoup
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import AppConfig
from .models import EmailMessage

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Google bisa mengembalikan scope dalam urutan/bentuk berbeda; jangan anggap sebagai error.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class GmailAuthError(Exception):
    """Token tidak ada/tidak valid; user perlu login ulang."""


class HistoryExpired(Exception):
    """startHistoryId tidak lagi dikenali Gmail (HTTP 404); perlu titik awal baru."""


# --- OAuth ---------------------------------------------------------------------


def _flow(cfg: AppConfig) -> Flow:
    # PKCE dimatikan: client web memakai client_secret, dan sesi Streamlit tidak bertahan
    # melewati redirect sehingga code_verifier tidak bisa disimpan di session_state.
    return Flow.from_client_config(
        cfg.google.client_config(),
        scopes=SCOPES,
        redirect_uri=cfg.google.redirect_uri,
        autogenerate_code_verifier=False,
    )


def _state_path(cfg: AppConfig) -> Path:
    return cfg.token_path.parent / "oauth_state"


def authorization_url(cfg: AppConfig) -> str:
    """URL consent Google. State disimpan di file lokal untuk dicek saat callback."""
    state = secrets.token_urlsafe(24)
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state, encoding="utf-8")
    url, _ = _flow(cfg).authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true", state=state
    )
    return url


def exchange_code(cfg: AppConfig, code: str, state: str | None) -> Credentials:
    """Tukar authorization code menjadi token dan simpan ke file lokal."""
    path = _state_path(cfg)
    expected = path.read_text(encoding="utf-8") if path.exists() else None
    if not expected or state != expected:
        raise GmailAuthError("State OAuth tidak cocok. Silakan ulangi login.")
    path.unlink(missing_ok=True)

    flow = _flow(cfg)
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_token(cfg, creds)
    return creds


def _save_token(cfg: AppConfig, creds: Credentials) -> None:
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(creds.to_json(), encoding="utf-8")


def load_credentials(cfg: AppConfig) -> Credentials | None:
    """Muat token tersimpan dan refresh bila perlu. None jika belum login / token tidak valid."""
    if not cfg.token_path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)
    except ValueError:
        logout(cfg)
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            logout(cfg)
            return None
        _save_token(cfg, creds)
        return creds
    logout(cfg)
    return None


def logout(cfg: AppConfig) -> None:
    cfg.token_path.unlink(missing_ok=True)


# --- Parsing pesan ---------------------------------------------------------------


def _decode(data: str | None, charset: str | None = None) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _charset(part: dict) -> str | None:
    for h in part.get("headers", []):
        if h["name"].lower() != "content-type":
            continue
        for param in h["value"].split(";")[1:]:
            key, _, value = param.strip().partition("=")
            if key.lower() == "charset":
                return value.strip().strip('"')
    return None


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _walk(part: dict, plain: list[str], html: list[str], attachments: list[str]) -> None:
    mime = part.get("mimeType", "")
    filename = part.get("filename")
    body = part.get("body", {})
    if filename:
        attachments.append(filename)
        return
    if mime == "text/plain":
        plain.append(_decode(body.get("data"), _charset(part)))
    elif mime == "text/html":
        html.append(_decode(body.get("data"), _charset(part)))
    for sub in part.get("parts", []) or []:
        _walk(sub, plain, html, attachments)


def parse_message(raw: dict) -> EmailMessage:
    """Ubah respons Gmail `users.messages.get(format=full)` menjadi EmailMessage."""
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    _walk(payload, plain, html, attachments)

    body = "\n\n".join(p for p in plain if p.strip()).strip()
    if not body and html:
        body = html_to_text("\n".join(html))

    return EmailMessage(
        message_id=raw["id"],
        thread_id=raw.get("threadId", ""),
        sender=headers.get("from", ""),
        subject=headers.get("subject", "(tanpa subject)"),
        received_at=int(raw.get("internalDate", "0")) // 1000,
        snippet=raw.get("snippet", ""),
        body_text=body,
        attachments=attachments,
    )


# --- Klien Gmail -------------------------------------------------------------------


class GmailClient:
    def __init__(self, creds: Credentials):
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _call(self, request):
        try:
            return request.execute()
        except RefreshError as e:
            raise GmailAuthError("Sesi Google kadaluarsa, silakan login ulang.") from e

    def profile_email(self) -> str:
        return self._call(self._service.users().getProfile(userId="me"))["emailAddress"]

    def current_history_id(self) -> str:
        """historyId mailbox saat ini; dipakai sebagai titik awal listener."""
        return str(self._call(self._service.users().getProfile(userId="me"))["historyId"])

    def list_added_since(self, history_id: str) -> tuple[list[str], str]:
        """ID email yang masuk ke INBOX setelah `history_id` (terlama dulu) dan historyId terbaru.

        Raise HistoryExpired jika Gmail tidak lagi mengenali `history_id`.
        """
        ids: list[str] = []
        seen: set[str] = set()
        latest = history_id
        page_token = None
        while True:
            request = self._service.users().history().list(
                userId="me",
                startHistoryId=history_id,
                historyTypes="messageAdded",
                labelId="INBOX",
                pageToken=page_token,
                maxResults=500,
            )
            try:
                resp = self._call(request)
            except HttpError as e:
                if e.resp.status == 404:
                    raise HistoryExpired(history_id) from e
                raise
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []) and msg["id"] not in seen:
                        seen.add(msg["id"])
                        ids.append(msg["id"])
            latest = str(resp.get("historyId", latest))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return ids, latest

    def fetch_message(self, message_id: str) -> EmailMessage:
        raw = self._call(self._service.users().messages().get(userId="me", id=message_id, format="full"))
        return parse_message(raw)
