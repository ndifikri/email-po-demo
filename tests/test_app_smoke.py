"""Smoke test UI dengan Streamlit AppTest (Gmail & LLM di-mock, DB sementara)."""
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from po_monitor.storage import Storage
from tests.helpers import make_email, po_analysis

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SECRETS = {
    "openai": {"api_key": "sk-test"},
    "google_oauth": {"client_id": "cid", "client_secret": "secret"},
}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    import po_monitor.config as config

    db = tmp_path / "app.db"
    token = tmp_path / ".secrets" / "gmail_token.json"
    orig = config.load_config

    def patched(secrets):
        import dataclasses

        return dataclasses.replace(orig(secrets), db_path=db, token_path=token)

    monkeypatch.setattr(config, "load_config", patched)
    return db, token


@pytest.fixture(autouse=True)
def fresh_resources():
    import streamlit as st

    st.cache_resource.clear()  # listener & storage baru per test
    yield
    st.cache_resource.clear()


def make_app():
    at = AppTest.from_file(APP, default_timeout=30)
    for k, v in SECRETS.items():
        at.secrets[k] = v
    return at


def test_login_page_when_no_token(paths):
    at = make_app().run()
    assert not at.exception
    assert "Login dengan Google" in "".join(m.value for m in at.markdown)


def test_missing_secret_shows_error(paths):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["openai"] = {}
    at.secrets["google_oauth"] = {"client_id": "cid", "client_secret": "s"}
    at.run()
    assert any("openai.api_key" in e.value for e in at.error)


class FakeGmail:
    def __init__(self, creds):
        pass

    def profile_email(self):
        return "tim@contoh.co.id"

    def current_history_id(self):
        return "1"

    def list_added_since(self, history_id):
        return [], "1"

    def fetch_message(self, message_id):
        raise AssertionError("tidak dipanggil")


def test_dashboard_with_data(paths):
    db, _ = paths
    store = Storage(db)
    store.save_email(make_email("m1", subject="PO Security"))
    store.save_result("m1", po_analysis(), "gpt-6-luna", 0.6)

    with patch("po_monitor.gmail_client.load_credentials", return_value=object()), patch(
        "po_monitor.gmail_client.GmailClient", FakeGmail
    ):
        at = make_app().run()
    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Email PO"] == "1"
    assert metrics["Total tenaga kerja"] == "5"
    assert len(at.dataframe) >= 1
    table = at.dataframe[0].value
    assert list(table.columns)[-1] == "Detail"
    assert table["Detail"].tolist() == ["5 Security untuk lokasi Cikarang"]
    assert any("Listener" in m.value for m in at.markdown)
    assert not any(b.label == "🔄 Cek sekarang" for b in at.button)
