"""Service-level helpers for the on-demand outbound SCIM pull.

Reuses the shared mapping module + ``update_user_attributes_from_source`` so
pulled attributes participate in the same multi-source provenance bookkeeping
as everything else.
"""

from __future__ import annotations

import logging

from constance import config

from waldur_auth_social.utils import update_user_attributes_from_source
from waldur_core.core.models import User
from waldur_core.users.scim.pull.client import ScimError, ScimPullClient
from waldur_core.users.scim.server.mapping import (
    scim_to_waldur_payload,
    set_scim_external_id,
)

logger = logging.getLogger(__name__)


class ScimPullConfigError(Exception):
    """Raised when SCIM_PULL_API_URL / API_KEY are not configured."""


def is_pull_configured() -> bool:
    return bool(config.SCIM_PULL_API_URL and config.SCIM_PULL_API_KEY)


def build_pull_client() -> ScimPullClient:
    if not is_pull_configured():
        raise ScimPullConfigError(
            "SCIM_PULL_API_URL and SCIM_PULL_API_KEY must be set in Constance."
        )
    return ScimPullClient(
        api_url=config.SCIM_PULL_API_URL,
        api_key=config.SCIM_PULL_API_KEY,
    )


def pull_user_attributes(
    user: User,
    *,
    client: ScimPullClient | None = None,
    source: str | None = None,
) -> set[str]:
    """Fetch a single user's SCIM record and merge it into ``user``.

    Returns the set of Waldur User fields that were actually changed.
    Raises ``ScimError`` if the remote returns a non-2xx response.
    Returns an empty set when the remote has no matching user.
    """
    if client is None:
        client = build_pull_client()
    source = source or config.SCIM_PULL_SOURCE_NAME or "scim:pull"

    remote = client.get_user_by_username(user.username)
    if remote is None:
        logger.info(
            "SCIM pull: remote has no user with userName=%s; skipping.",
            user.username,
        )
        return set()

    payload = scim_to_waldur_payload(remote)
    allowed = set(config.SCIM_INBOUND_ALLOWED_ATTRIBUTES or [])
    changed = update_user_attributes_from_source(
        user, payload, source=source, allowed_fields=allowed
    )

    external_id = remote.get("externalId")
    if external_id:
        set_scim_external_id(user, str(external_id), source=source)
        user.save(update_fields=["attribute_sources"])
        changed = changed | {"externalId"}

    return changed


__all__ = [
    "ScimError",
    "ScimPullConfigError",
    "build_pull_client",
    "is_pull_configured",
    "pull_user_attributes",
]
