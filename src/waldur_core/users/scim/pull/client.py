"""SCIM client extensions for on-demand pull.

Extends the existing ``waldur_core.users.scim.client.ScimClient`` (used for
entitlement push) with read operations needed to fetch user records from a
remote SCIM 2.0 directory.
"""

from __future__ import annotations

from urllib.parse import urlencode

from waldur_core.users.scim.client import ScimClient, ScimError


class ScimPullClient(ScimClient):
    """Adds ``GET /Users`` filtering and pagination helpers."""

    def get_user_by_username(self, user_name: str) -> dict | None:
        """Return the first SCIM User resource whose ``userName`` matches.

        Returns ``None`` when no user is found (rather than raising) so callers
        can decide whether absence is an error.
        """
        # We use the filter form because SCIM ``id`` differs from userName
        # across implementations.
        escaped = user_name.replace('"', '\\"')
        query = urlencode({"filter": f'userName eq "{escaped}"'})
        response = self._request("GET", f"Users?{query}")
        resources = response.get("Resources") or []
        if not resources:
            return None
        return resources[0]

    def list_users(
        self,
        *,
        start_index: int = 1,
        count: int = 100,
        filter: str | None = None,
    ) -> dict:
        """Paginated ``GET /Users`` — returns the raw ListResponse body."""
        params: dict[str, str] = {
            "startIndex": str(start_index),
            "count": str(count),
        }
        if filter:
            params["filter"] = filter
        query = urlencode(params)
        return self._request("GET", f"Users?{query}")


__all__ = ["ScimPullClient", "ScimError"]
