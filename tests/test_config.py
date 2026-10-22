import pytest

from po_monitor.config import ConfigError, load_config

BASE = {
    "openai": {"api_key": "sk-test"},
    "google_oauth": {"client_id": "cid", "client_secret": "csecret"},
    "auth": {"username": "admin", "password": "test-pass#1"},
}


def test_defaults_applied():
    cfg = load_config(BASE)
    assert cfg.model == "gpt-6-luna"
    assert cfg.poll_interval_seconds == 15
    assert cfg.max_emails_per_cycle == 20
    assert cfg.confidence_threshold == 0.6
    assert cfg.max_body_chars == 12000
    assert cfg.google.redirect_uri == "http://localhost:8501"
    assert cfg.admin_username == "admin"
    assert cfg.admin_password == "test-pass#1"


def test_overrides():
    secrets = {**BASE, "openai": {"api_key": "sk", "model": "other"}, "app": {"poll_interval_seconds": 30}}
    cfg = load_config(secrets)
    assert cfg.model == "other"
    assert cfg.poll_interval_seconds == 30


def test_missing_keys_reported_without_values():
    with pytest.raises(ConfigError) as exc:
        load_config({"openai": {}, "google_oauth": {"client_id": "cid"}})
    assert exc.value.missing == [
        "openai.api_key",
        "google_oauth.client_secret",
        "auth.username",
        "auth.password",
    ]
    assert "cid" not in str(exc.value)


def test_missing_auth_password_reported_without_values():
    with pytest.raises(ConfigError) as exc:
        load_config({**BASE, "auth": {"username": "admin"}})
    assert exc.value.missing == ["auth.password"]
    assert "admin" not in str(exc.value)
