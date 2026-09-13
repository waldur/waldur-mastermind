"""Single-use tickets that open one browser shell session.

A ticket is signed with SECRET_KEY, names the staff user and carries a random
nonce. When the link was requested with a Waldur API token, it also carries a
digest of that token, so the server can end the session once the token is gone
(logout deletes it). The API mints tickets; the web shell server, a separate
process sharing the same settings, checks their age and redeems each nonce once.
"""

import hashlib
import hmac
import secrets
from typing import NamedTuple

from django.conf import settings
from django.core import signing

SALT = "waldur_core.web_shell.ticket"
MAX_AGE = 60


class Ticket(NamedTuple):
    user_uuid: str
    nonce: str
    # SHA-256 of the API token the link was requested with; None when the
    # request was authenticated some other way (PAT, OIDC bearer).
    token_digest: str | None


def is_enabled() -> bool:
    core = settings.WALDUR_CORE
    return bool(
        settings.DEBUG and core.get("WEB_SHELL_ENABLED") and core.get("WEB_SHELL_URL")
    )


def token_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def token_matches(key: str, digest: str) -> bool:
    return hmac.compare_digest(token_digest(key), digest)


def mint(user, token_key: str | None = None) -> str:
    payload = {"u": user.uuid.hex, "n": secrets.token_urlsafe(16)}
    if token_key:
        # A digest, never the key itself: the ticket is signed, not encrypted.
        payload["k"] = token_digest(token_key)
    return signing.dumps(payload, salt=SALT)


def load(ticket: str, max_age: int = MAX_AGE) -> Ticket | None:
    """The ticket's claims, or None for a forged, stale or malformed ticket."""
    try:
        data = signing.loads(ticket, salt=SALT, max_age=max_age)
    except signing.BadSignature:  # SignatureExpired is a subclass
        return None
    if not isinstance(data, dict) or not data.get("u") or not data.get("n"):
        return None
    return Ticket(data["u"], data["n"], data.get("k"))


def build_url(ticket: str) -> str:
    # The fragment is never sent to a server, so the ticket stays out of
    # access logs; the page removes it from the address bar on load.
    return f"{settings.WALDUR_CORE['WEB_SHELL_URL']}#t={ticket}"
