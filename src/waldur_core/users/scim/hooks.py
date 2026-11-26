"""Extension registry for marketplace data used by outbound SCIM entitlement tasks.

``waldur_mastermind.marketplace`` registers its provider here from
``AppConfig.ready()`` so ``waldur_core.users`` does not import marketplace models.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScimOfferingProvider:
    """Marketplace queries required by outbound SCIM entitlement synchronization.

    ``get_user_offering_usernames(user, project_ids)`` returns:
    - ``None`` when no SCIM-enabled offerings have active resources in ``project_ids``.
    - ``{}`` when eligible offerings exist but ``user`` has no OK offering users.
    - ``{offering: username}`` otherwise.
    """

    get_user_offering_usernames: Callable[[Any, Any], dict[Any, str] | None]
    get_reconciliation_user_ids: Callable[[Any], set[int]]
    get_offering: Callable[[str], Any | None]
    get_offering_eligible_user_uuids: Callable[[Any], list[str]]


_scim_offering_provider: ScimOfferingProvider | None = None


def register_scim_offering_provider(provider: ScimOfferingProvider) -> None:
    """Register the provider supplying offering/resource data for SCIM tasks."""
    global _scim_offering_provider
    _scim_offering_provider = provider


def get_scim_offering_provider() -> ScimOfferingProvider | None:
    return _scim_offering_provider
