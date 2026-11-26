"""Extension registries for external identity/auth providers.

Auth integration apps (such as ``waldur_auth_social``) register their hooks
here from ``AppConfig.ready()`` so ``waldur_core.core`` does not import
provider integration packages directly.
"""

from collections.abc import Callable
from typing import Any

_logout_hooks: list[Callable[[Any], None]] = []
_oidc_logout_url_provider: Callable[[str], str | None] | None = None
_default_idp_provider: Callable[[str], dict[str, Any] | None] | None = None
_identity_provider_lookup: Callable[[str], Any] | None = None
_remote_user_pullers: dict[str, Callable[[str], Any]] = {}


def register_logout_hook(hook: Callable[[Any], None]) -> None:
    """Register a callback invoked with ``user`` before session/token logout."""
    if hook not in _logout_hooks:
        _logout_hooks.append(hook)


def run_logout_hooks(user: Any) -> None:
    for hook in _logout_hooks:
        hook(user)


def register_oidc_logout_url_provider(
    provider: Callable[[str], str | None],
) -> None:
    """Register ``provider(authentication_method) -> logout_url | None``."""
    global _oidc_logout_url_provider
    _oidc_logout_url_provider = provider


def get_oidc_logout_url(authentication_method: str) -> str | None:
    if _oidc_logout_url_provider is None:
        return None
    return _oidc_logout_url_provider(authentication_method)


def register_default_idp_provider(
    provider: Callable[[str], dict[str, Any] | None],
) -> None:
    """Register ``provider(provider_name) -> dict | None`` for public settings."""
    global _default_idp_provider
    _default_idp_provider = provider


def get_default_idp_info(provider_name: str) -> dict[str, Any] | None:
    if _default_idp_provider is None:
        return None
    return _default_idp_provider(provider_name)


def register_identity_provider_lookup(lookup: Callable[[str], Any]) -> None:
    """Register ``lookup(registration_method) -> IdentityProvider | None``."""
    global _identity_provider_lookup
    _identity_provider_lookup = lookup


def get_identity_provider(registration_method: str) -> Any:
    if _identity_provider_lookup is None:
        return None
    return _identity_provider_lookup(registration_method)


def register_remote_user_puller(
    provider: str,
    puller: Callable[[str], Any],
) -> None:
    """Register ``puller(username)`` for syncing a remote user account."""
    _remote_user_pullers[provider] = puller


def has_remote_user_puller(provider: str) -> bool:
    """Return True when a remote-user puller is registered for ``provider``."""
    return provider in _remote_user_pullers


def pull_remote_user(provider: str, username: str) -> Any:
    puller = _remote_user_pullers.get(provider)
    if puller is None:
        return None
    return puller(username)
