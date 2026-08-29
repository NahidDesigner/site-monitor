"""Session cookies for the dashboard.

A signed cookie rather than server-side sessions: there is one user, the
process restarts on every redeploy, and an HMAC needs no storage and no extra
dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "sm_session"


def _sign(secret: str, payload: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")


def issue(secret: str, *, hours: int) -> str:
    """Mint a session token valid for `hours`."""
    expires = int(time.time()) + hours * 3600
    payload = f"{expires}"
    return f"{payload}.{_sign(secret, payload)}"


def verify(secret: str, token: str | None) -> bool:
    """True when the token is well-formed, correctly signed and unexpired."""
    if not token or "." not in token:
        return False
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def password_matches(expected: str | None, supplied: str) -> bool:
    """Constant-time password check."""
    if not expected:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


def new_secret() -> str:
    return secrets.token_urlsafe(32)
