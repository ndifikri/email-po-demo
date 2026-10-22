import pytest

from po_monitor.auth import check_credentials
from po_monitor.config import load_config

CFG = load_config(
    {
        "openai": {"api_key": "sk-test"},
        "google_oauth": {"client_id": "cid", "client_secret": "csecret"},
        "auth": {"username": "admin", "password": "test-pass#1"},
    }
)


def test_correct_credentials():
    assert check_credentials(CFG, "admin", "test-pass#1")


@pytest.mark.parametrize(
    "username,password",
    [
        ("admin", "Test-pass#1"),
        ("admin", "test-pass"),
        ("Admin", "test-pass#1"),
        ("root", "test-pass#1"),
        ("", ""),
        ("admin", ""),
    ],
)
def test_wrong_credentials(username, password):
    assert not check_credentials(CFG, username, password)
