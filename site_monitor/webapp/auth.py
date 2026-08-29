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


class LoginThrottle:
    """Slows down password guessing against a public dashboard.

    One password on a public URL is only as good as the number of guesses an
    attacker gets. Two cheap measures, no dependencies and no storage:

    * every failed attempt costs a fixed delay, which makes online brute force
      impractical regardless of where it comes from;
    * too many failures from one client locks that client out for a while.

    State is in memory, so a restart clears it. That is an acceptable trade for
    a single-operator tool: the delay is the part that does the real work, and
    the lockout is a backstop. The window is deliberately short so that someone
    hammering the login cannot keep the owner locked out for long.
    """

    def __init__(self, *, max_attempts: int = 10, window_seconds: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        recent = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def locked_for(self, key: str, *, now: float | None = None) -> int:
        """Seconds remaining before this client may try again; 0 if allowed."""
        now = time.time() if now is None else now
        recent = self._recent(key, now)
        if len(recent) < self.max_attempts:
            return 0
        return max(0, int(recent[0] + self.window_seconds - now))

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._recent(key, now)
        self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def client_key(request) -> str:
    """Best available identity for the caller.

    Behind Coolify's proxy every request arrives from the same address, so the
    forwarded header is used when present. It is spoofable, which is why the
    per-attempt delay — which no header can dodge — carries the real weight.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
