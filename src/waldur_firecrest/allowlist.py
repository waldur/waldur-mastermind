"""Staff allowlists of the endpoints that receive a user's token.

``FIRECREST_ALLOWED_API_URLS`` and ``SLURM_MONITOR_ALLOWED_API_URLS`` hold one
entry per line (or comma-separated): a URL, optionally followed by the
audience to exchange the user's token for before sending it there::

    https://firecrest.example.org
    https://firecrest.example.org|firecrest
    https://firecrest.example.org|firecrest|firecrest-audience

With an audience, Waldur asks the identity provider for a token issued for
that client only (RFC 8693 token exchange), requesting the client scope named
in the third field, or the audience itself when it is left out; in Keycloak
that optional client scope carries the audience mapper. The endpoint then
never sees the user's sign-in token, and the token it gets is good for that
service only. Without an audience the sign-in token is sent, as before.

Staff, not providers, choose the audience: a provider names only the URL on
its offering, and a token is minted only for an allowlisted one.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenAudience:
    audience: str
    scope: str


def parse(value) -> dict[str, TokenAudience | None]:
    entries = {}
    for entry in re.split(r"[,\s]+", value or ""):
        entry = entry.strip()
        if not entry:
            continue
        url, _, rest = entry.partition("|")
        audience, _, scope = rest.partition("|")
        entries[url.rstrip("/")] = (
            TokenAudience(audience, scope or audience) if audience else None
        )
    return entries
