"""Shared helpers for SCIM endpoint tests."""

from __future__ import annotations

from rest_framework.authtoken.models import Token

from waldur_core.structure.tests import factories as structure_factories


def make_staff_token() -> tuple[str, object]:
    """Create a staff service-account user + AuthToken and return the token key.

    Uses ``get_or_create`` because some Waldur code paths create a token as a
    side effect of user creation; tests should be robust to that.
    """
    user = structure_factories.UserFactory(
        is_staff=True, is_active=True, username="scim-svc"
    )
    token, _ = Token.objects.get_or_create(user=user)
    return token.key, user


def auth_headers(token_key: str) -> dict:
    return {
        "HTTP_AUTHORIZATION": f"Bearer {token_key}",
        "HTTP_ACCEPT": "application/scim+json",
    }
