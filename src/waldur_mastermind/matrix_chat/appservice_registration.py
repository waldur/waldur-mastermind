import re

from . import models, serializers


def build_registration(*, url, as_token, hs_token, sender_localpart, homeserver_domain):
    """Build the appservice registration a homeserver installs.

    Every registration Waldur hands out comes from here, so the Setup endpoint
    and the generate_appservice_registration command cannot declare different
    namespaces.

    Raises ValidationError when the localpart or domain would widen a namespace
    regex beyond Waldur's own identities.
    """
    serializers.validate_sender_localpart(sender_localpart)
    serializers.validate_homeserver_domain(homeserver_domain)

    return {
        "id": "waldur",
        "url": url,
        "as_token": as_token,
        "hs_token": hs_token,
        "sender_localpart": sender_localpart,
        "namespaces": {
            "users": [
                # Claim the bot identity exclusively so it cannot be
                # registered through normal client signup on the local
                # homeserver. The bot always lives on
                # MATRIX_HOMESERVER_DOMAIN, so the regex is scoped.
                {
                    "exclusive": True,
                    "regex": f"@{sender_localpart}:{homeserver_domain}",
                },
                {
                    "exclusive": False,
                    "regex": f"@.*:{homeserver_domain}",
                },
            ],
            "rooms": [],
            # An appservice may only create aliases inside its declared
            # namespace. Left empty, the homeserver answers M_EXCLUSIVE to
            # every alias request, room creation catches that and silently
            # creates the room without one, and the "Open in Matrix client"
            # link — which renders only when an alias exists — never
            # appears. Claimed exclusively for the same reason as the bot
            # user: nobody else should be able to take an address Waldur
            # hands out. `[^:]+` rather than `.*` so the wildcard cannot
            # swallow the separator and reach another domain, and the
            # domain is escaped so its dots are literal.
            "aliases": [
                {
                    "exclusive": True,
                    "regex": f"#{models.ROOM_ALIAS_PREFIX}[^:]+:{re.escape(homeserver_domain)}",
                },
            ],
        },
    }
