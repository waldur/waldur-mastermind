"""Extension registries for integration apps plugging into ``waldur_mastermind.chat``.

Higher-layer integration apps (such as ``waldur_firecrest`` and
``waldur_mastermind.marketplace_openstack``) register their chat tools and
workspace providers from ``AppConfig.ready()`` so ``waldur_mastermind.chat``
does not import upward across layers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HpcWorkspaceProvider:
    """Provider for HPC workspace availability and user allocations (e.g. ``waldur_firecrest``)."""

    is_configured: Callable[[], bool]
    resources_for_user: Callable[[Any], Any]


_hpc_workspace_provider: HpcWorkspaceProvider | None = None


def register_hpc_workspace_provider(provider: HpcWorkspaceProvider) -> None:
    """Register HPC workspace configuration and allocation lookup callbacks."""
    global _hpc_workspace_provider
    _hpc_workspace_provider = provider


def get_hpc_workspace_provider() -> HpcWorkspaceProvider | None:
    return _hpc_workspace_provider


def is_hpc_configured() -> bool:
    if _hpc_workspace_provider is None:
        return False
    return _hpc_workspace_provider.is_configured()


def get_hpc_resources_for_user(user: Any) -> Any | None:
    if _hpc_workspace_provider is None:
        return None
    return _hpc_workspace_provider.resources_for_user(user)
