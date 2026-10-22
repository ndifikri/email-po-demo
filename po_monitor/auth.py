"""Validasi login admin aplikasi (kredensial dari st.secrets["auth"])."""
from __future__ import annotations

import hmac

from .config import AppConfig


def check_credentials(cfg: AppConfig, username: str, password: str) -> bool:
    """True jika username & password cocok dengan secrets; dibandingkan constant-time."""
    user_ok = hmac.compare_digest(username.encode("utf-8"), cfg.admin_username.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), cfg.admin_password.encode("utf-8"))
    return user_ok and pass_ok
