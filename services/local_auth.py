"""Minimal local access control for the workbench."""
from __future__ import annotations

import hmac
from urllib.parse import urlsplit


DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "taobaoadmin@1"
SESSION_USER_KEY = "workbench_user"


def credentials(env) -> tuple[str, str]:
    """Read local overrides without placing credentials in source control."""
    return (
        env.get("WORKBENCH_ADMIN_USERNAME") or DEFAULT_USERNAME,
        env.get("WORKBENCH_ADMIN_PASSWORD") or DEFAULT_PASSWORD,
    )


def verify(username: str | None, password: str | None, env) -> bool:
    expected_username, expected_password = credentials(env)
    return (
        hmac.compare_digest(username or "", expected_username)
        and hmac.compare_digest(password or "", expected_password)
    )


def sign_in(session, username: str) -> None:
    session[SESSION_USER_KEY] = username


def signed_in(session) -> bool:
    return bool(session.get(SESSION_USER_KEY))


def sign_out(session) -> None:
    session.clear()


def safe_next_path(value: str | None) -> str:
    """Return an internal redirect target, never an external URL."""
    if not value or "\r" in value or "\n" in value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    return value
