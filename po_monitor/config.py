"""Konfigurasi aplikasi. Satu-satunya modul yang membaca st.secrets."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gpt-6-luna"

REQUIRED_KEYS = [
    ("openai", "api_key"),
    ("google_oauth", "client_id"),
    ("google_oauth", "client_secret"),
]


class ConfigError(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Secret wajib tidak ditemukan: " + ", ".join(missing))


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    project_id: str
    auth_uri: str
    token_uri: str
    redirect_uri: str

    def client_config(self) -> dict:
        """Format client config yang diharapkan google_auth_oauthlib.Flow."""
        return {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "project_id": self.project_id,
                "auth_uri": self.auth_uri,
                "token_uri": self.token_uri,
                "redirect_uris": [self.redirect_uri],
            }
        }


@dataclass(frozen=True)
class AppConfig:
    openai_api_key: str
    model: str
    google: GoogleOAuthConfig
    poll_interval_seconds: int = 15
    max_emails_per_cycle: int = 20
    confidence_threshold: float = 0.6
    max_body_chars: int = 12000
    db_path: Path = PROJECT_ROOT / "data" / "po_monitor.db"
    token_path: Path = PROJECT_ROOT / ".secrets" / "gmail_token.json"


def load_config(secrets: Mapping) -> AppConfig:
    """Bangun AppConfig dari mapping secrets; raise ConfigError jika key wajib hilang."""
    missing = [
        f"{section}.{key}"
        for section, key in REQUIRED_KEYS
        if not (secrets.get(section) or {}).get(key)
    ]
    if missing:
        raise ConfigError(missing)

    openai_cfg = secrets["openai"]
    g = secrets["google_oauth"]
    app = secrets.get("app") or {}

    return AppConfig(
        openai_api_key=openai_cfg["api_key"],
        model=openai_cfg.get("model") or DEFAULT_MODEL,
        google=GoogleOAuthConfig(
            client_id=g["client_id"],
            client_secret=g["client_secret"],
            project_id=g.get("project_id", ""),
            auth_uri=g.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            token_uri=g.get("token_uri", "https://oauth2.googleapis.com/token"),
            redirect_uri=g.get("redirect_uri", "http://localhost:8501"),
        ),
        poll_interval_seconds=int(app.get("poll_interval_seconds", 15)),
        max_emails_per_cycle=int(app.get("max_emails_per_cycle", 20)),
        confidence_threshold=float(app.get("confidence_threshold", 0.6)),
        max_body_chars=int(app.get("max_body_chars", 12000)),
    )


def get_config() -> AppConfig:
    """Muat config dari st.secrets; tampilkan error dan hentikan app jika tidak lengkap."""
    import streamlit as st

    try:
        return load_config(st.secrets)
    except ConfigError as e:
        st.error(
            f"{e}. Lengkapi `.streamlit/secrets.toml` "
            "(lihat `.streamlit/secrets.toml.example`)."
        )
        st.stop()
    except FileNotFoundError:
        st.error("File `.streamlit/secrets.toml` tidak ditemukan.")
        st.stop()
